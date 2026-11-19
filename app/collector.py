"""Collection pipeline: run a dataset, parse metrics, evaluate rules, persist
the latest value, derive node status, and fire email alerts.

This is the heart of the monitor. It is called by the scheduler for scheduled
runs and by the API for manual "run now" / "test" actions.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db, executor, mailer, rules as rules_mod, templates
from .crypto import secret_box
from .parser import ParsedValue, extract


def refresh_metric_status(conn: sqlite3.Connection, metric_id: int) -> None:
    """Re-derive a metric's stored status from its current rules + latest value.

    ``metric_values.status_level_id`` is normally written by :func:`collect_dataset`,
    but editing a metric's rules (add/remove) changes what status its *existing*
    value maps to without any new collection. Re-evaluating here keeps the stored
    status — and therefore the node rollup — consistent immediately after a rule
    edit instead of waiting for the next scheduled run.
    """
    m = conn.execute("SELECT id, dataset_id FROM metrics WHERE id=?", (metric_id,)).fetchone()
    if not m:
        return
    val = conn.execute("SELECT raw, num, txt FROM metric_values WHERE metric_id=?", (metric_id,)).fetchone()
    ds = conn.execute("SELECT * FROM datasets WHERE id=?", (m["dataset_id"],)).fetchone()
    pvals = _param_values(conn, ds) if ds else {}
    levels, unknown_id = _load_status_levels(conn)
    ranks = {lid: lv["rank"] for lid, lv in levels.items()}
    if val is None:
        # No value collected yet; nothing to evaluate.
        sid = None
    else:
        pv = ParsedValue(val["raw"], val["num"], val["txt"])
        rule_rows = [
            rules_mod.RuleRow(r["id"], r["op"], templates.render_params(r["value"] or "", pvals),
                              r["status_level_id"], r["priority"])
            for r in conn.execute("SELECT * FROM rules WHERE metric_id=?", (metric_id,)).fetchall()
        ]
        sid = rules_mod.evaluate_metric(rule_rows, pv, ranks)
        if sid is None:
            sid = unknown_id
    conn.execute(
        "UPDATE metric_values SET status_level_id=? WHERE metric_id=?",
        (sid, metric_id),
    )
    if ds:
        recompute_node_status(conn, ds["node_id"])


@dataclass
class MetricOutcome:
    metric_id: int
    name: str
    value: ParsedValue
    status_level_id: int | None


def _load_status_levels(conn: sqlite3.Connection) -> tuple[dict[int, dict], int | None]:
    rows = conn.execute("SELECT id, name, color, rank, is_default FROM status_levels").fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    unknown_id = next((r["id"] for r in rows if r["name"] == "unknown"), None)
    return by_id, unknown_id


def _get_secret(conn: sqlite3.Connection, credential_id: int | None) -> tuple[str | None, str | None]:
    if not credential_id:
        return None, None
    row = conn.execute("SELECT username, secret_enc FROM credentials WHERE id=?", (credential_id,)).fetchone()
    if not row:
        return None, None
    return secret_box().decrypt(row["secret_enc"]), row["username"]


def _param_values(conn: sqlite3.Connection, ds) -> dict[str, str]:
    """Build the ``[Param]`` resolution map for a dataset at execution time.

    Node-derived parameters (Host/Port/Protocol/…) seed the map, then the
    dataset's saved ``param_values`` override them (case-insensitively). A
    token with no entry is left untouched by :func:`templates.render_params`.
    """
    node = conn.execute("SELECT * FROM nodes WHERE id=?", (ds["node_id"],)).fetchone()
    vals: dict[str, str] = {}
    if node is not None:
        vals.update(templates.known_params(node))
    try:
        saved = json.loads((ds["param_values"] if "param_values" in ds.keys() else "{}") or "{}")
    except (ValueError, TypeError):
        saved = {}
    if isinstance(saved, dict):
        # ``render_params`` looks tokens up by lower-cased name, so store the
        # saved overrides under a lower-cased key too (known_params already is).
        # Self-referential entries (a value equal to its own ``[Token]``, e.g.
        # {"label": "[label]"}) are dropped: they would shadow the node-derived
        # value with the literal token, leaving e.g. "ping [label]" unresolved.
        # Node-derived keys are never persisted anymore (the admin strips them
        # and a migration cleans old rows), so a saved custom override of the
        # same name is intentional and wins; a stale duplicate cannot occur.
        for k, v in templates.strip_self_referential(saved).items():
            vals[str(k).lower()] = "" if v is None else str(v)
    return vals


def _resolve(conn: sqlite3.Connection, ds, text) -> str:
    """Fully resolve a stored template string for execution: ``{{mustache}}``
    node fields first, then ``[Param]`` tokens."""
    node = conn.execute("SELECT * FROM nodes WHERE id=?", (ds["node_id"],)).fetchone()
    ctx = templates.node_context(node) if node is not None else {}
    return templates.render_params(templates.render(text or "", ctx), _param_values(conn, ds))


def collect_dataset(conn: sqlite3.Connection, dataset_id: int, *, persist: bool = True,
                    source: str = "scheduled") -> dict:
    """Run one dataset and update latest values + node status.

    ``source`` is ``"scheduled"`` for timer-driven runs (the default) or
    ``"manual"`` for on-demand runs (the ``/run`` endpoints). It only affects
    alert streak accounting: manual runs never advance or reset the consecutive
    problem-run counter that gates ``trigger_count``.

    Returns a summary dict suitable for both persistence and the test endpoint.
    """
    ds = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if not ds:
        return {"ok": False, "error": "dataset not found", "metrics": []}

    secret, username = _get_secret(conn, ds["credential_id"])
    script = _resolve(conn, ds, ds["script"])
    result = executor.run_dataset(ds["method"], script, ds["timeout_s"], secret, username)

    levels, unknown_id = _load_status_levels(conn)
    ranks = {lid: lv["rank"] for lid, lv in levels.items()}
    outcomes: list[MetricOutcome] = []

    pvals = _param_values(conn, ds)
    metrics = conn.execute("SELECT * FROM metrics WHERE dataset_id=?", (dataset_id,)).fetchall()
    for m in metrics:
        regex = templates.render_params(m["regex"] or "", pvals)
        value = extract(result.raw, regex, m["group_index"], m["cast"])
        rule_rows = [
            rules_mod.RuleRow(r["id"], r["op"], templates.render_params(r["value"] or "", pvals),
                              r["status_level_id"], r["priority"])
            for r in conn.execute("SELECT * FROM rules WHERE metric_id=?", (m["id"],)).fetchall()
        ]
        status_id = rules_mod.evaluate_metric(rule_rows, value, ranks)
        if status_id is None:
            status_id = unknown_id
        outcomes.append(MetricOutcome(m["id"], m["name"], value, status_id))

        if persist:
            conn.execute(
                """INSERT INTO metric_values (metric_id, dataset_id, raw, num, txt, status_level_id, collected_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(metric_id) DO UPDATE SET
                     raw=excluded.raw, num=excluded.num, txt=excluded.txt,
                     status_level_id=excluded.status_level_id, collected_at=excluded.collected_at""",
                (m["id"], dataset_id, value.raw, value.num, value.txt, status_id, db.now_iso()),
            )
            _maybe_alert(conn, m, status_id, value, levels, source=source)

    if persist:
        conn.execute(
            "UPDATE datasets SET last_run_at=?, last_status=?, last_error=?, last_output=?, next_run_at=? WHERE id=?",
            (db.now_iso(), "ok" if result.ok else "error", result.error, result.raw,
             _next_run(ds["interval_s"]), dataset_id),
        )
        node_status = recompute_node_status(conn, ds["node_id"])
        return {
            "ok": result.ok,
            "raw": result.raw,
            "error": result.error,
            "node_status": node_status,
            "metrics": [_outcome_dict(o, levels) for o in outcomes],
        }

    return {
        "ok": result.ok,
        "raw": result.raw,
        "error": result.error,
        "metrics": [_outcome_dict(o, levels) for o in outcomes],
    }


def _outcome_dict(o: MetricOutcome, levels: dict[int, dict]) -> dict:
    lvl = levels.get(o.status_level_id) if o.status_level_id else None
    return {
        "metric_id": o.metric_id,
        "name": o.name,
        "raw": o.value.raw,
        "num": o.value.num,
        "txt": o.value.txt,
        "error": o.value.error,
        "status": lvl["name"] if lvl else None,
        "status_color": lvl["color"] if lvl else None,
    }


def recompute_node_status(conn: sqlite3.Connection, node_id: str) -> str | None:
    """Node status = worst rank across enabled datasets' metric values."""
    rows = conn.execute(
        """SELECT mv.status_level_id AS sid
           FROM metric_values mv
           JOIN datasets d ON d.id = mv.dataset_id
           WHERE d.node_id=? AND d.enabled=1 AND mv.status_level_id IS NOT NULL""",
        (node_id,),
    ).fetchall()
    levels, unknown_id = _load_status_levels(conn)
    ranks = {lid: lv["rank"] for lid, lv in levels.items()}
    # Only good/warning/critical contribute to the rollup; unknown never does.
    contrib = [r["sid"] for r in rows if r["sid"] != unknown_id]
    sid = rules_mod.worst_status(contrib, ranks)
    if sid is None:
        sid = unknown_id
    conn.execute(
        "UPDATE nodes SET status_level_id=?, status_updated_at=? WHERE id=?",
        (sid, db.now_iso(), node_id),
    )
    lvl = levels.get(sid) if sid else None
    return lvl["name"] if lvl else None


def _trigger_statuses(cfg: sqlite3.Row) -> set[str]:
    """The set of status names that qualify a scheduled run to increment the alert
    streak (``alert_configs.trigger_statuses``, a JSON array). Defaults to
    {"warning","critical"} when unset or malformed, matching the historical behaviour."""
    raw = cfg["trigger_statuses"] if "trigger_statuses" in cfg.keys() else None
    if not raw:
        return {"warning", "critical"}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"warning", "critical"}
    if isinstance(parsed, list) and parsed:
        return {str(s) for s in parsed}
    return {"warning", "critical"}


def _maybe_alert(conn: sqlite3.Connection, metric: sqlite3.Row, status_id: int | None,
                 value: ParsedValue, levels: dict[int, dict], *, source: str = "scheduled") -> None:
    cfg = conn.execute("SELECT * FROM alert_configs WHERE metric_id=?", (metric["id"],)).fetchone()
    if not cfg or not cfg["enabled"]:
        return
    lvl = levels.get(status_id) if status_id else None
    status_name = lvl["name"] if lvl else "unknown"
    qualifying = _trigger_statuses(cfg)

    # Consecutive scheduled qualifying-run streak. Only scheduled runs touch it:
    # a manual/on-demand run neither advances nor resets the counter, so a
    # transient blip caught by hand cannot trigger (or clear) an email. A
    # scheduled run in a non-qualifying status (e.g. good, or any status outside
    # the configured trigger_statuses) resets the streak to 0 and never alerts.
    streak = cfg["trigger_streak"] if "trigger_streak" in cfg.keys() else 0
    if source == "scheduled":
        if status_name in qualifying:
            streak += 1
        else:
            streak = 0
        conn.execute("UPDATE alert_configs SET trigger_streak=? WHERE metric_id=?",
                     (streak, metric["id"]))

    # Only alert on qualifying statuses; any other status never pages anyone.
    # Honour only_on_change.
    if status_name not in qualifying:
        return
    # Require the configured number of consecutive scheduled problem runs before
    # the first email (trigger_count=1 keeps the historical alert-on-first-run).
    # This gate applies to scheduled runs only: a manual/on-demand run is fully
    # neutral to streak accounting and alerts immediately, without advancing or
    # resetting the counter.
    required = max(1, cfg["trigger_count"] if "trigger_count" in cfg.keys() else 1)
    if source == "scheduled" and streak < required:
        return
    if cfg["only_on_change"] and cfg["last_status"] == status_name:
        return
    if cfg["cooldown_s"] and cfg["last_sent_at"]:
        import time
        last = time.mktime(time.strptime(cfg["last_sent_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if time.time() - last < cfg["cooldown_s"]:
            return
    node_id = _metric_node(conn, metric["id"])
    node = conn.execute("SELECT label, network_path, network_resource FROM nodes WHERE id=?", (node_id,)).fetchone()
    ctx = _alert_ctx(node, metric, status_name, value)
    # Subject/body fall back to the global defaults, then to built-ins, so an
    # alert can leave them blank and still get a generated, name/status-based message.
    subject = cfg["subject"] or db.get_setting(conn, "alert_default_subject") or "Status alert: {{status}}"
    body = cfg["body"] or db.get_setting(conn, "alert_default_body") or \
        "Metric {{metric.name}} on {{node.label}} is {{status}} (value={{value}})."
    subject = mailer.render(subject, ctx)
    body = mailer.render(body, ctx)
    sent = mailer.send_email(conn, cfg["recipients"], subject, body)
    conn.execute(
        "UPDATE alert_configs SET last_status=?, last_sent_at=? WHERE metric_id=?",
        (status_name, db.now_iso() if sent else cfg["last_sent_at"], metric["id"]),
    )


def _metric_node(conn: sqlite3.Connection, metric_id: int) -> str | None:
    row = conn.execute(
        "SELECT d.node_id FROM datasets d JOIN metrics m ON m.dataset_id=d.id WHERE m.id=?",
        (metric_id,),
    ).fetchone()
    return row["node_id"] if row else None


def _alert_ctx(node, metric, status_name: str, value: ParsedValue) -> dict:
    return {
        "node": {"label": node["label"] if node else "", "network_path": node["network_path"] if node else "", "network_resource": node["network_resource"] if node else ""},
        "metric": {"name": metric["name"]},
        "status": status_name,
        "value": value.txt if value.txt is not None else (value.raw or ""),
    }


def _next_run(interval_s: int) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(1, interval_s)))
