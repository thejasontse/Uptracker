"""FastAPI application: JSON API + static frontend.

Endpoints (all JSON):

* ``GET  /api/snapshot``            — everything the UI polls (graph, nodes,
  statuses, latest metric values, status levels).
* ``PUT  /api/graph``               — persist the nodegraph layout; node rows
  are synced (created/pruned) against the model.
* ``PATCH/DELETE /api/nodes/{id}``  — node metadata + tracking opt-in.
* datasets / metrics / rules / alerts CRUD.
* ``POST /api/datasets/{id}/run``   — force a collection now.
* ``POST /api/test/run``            — dry-run a draft dataset (no persistence).
* credentials CRUD — secrets are write-only (never returned).
* templates CRUD + ``POST /api/templates/{id}/apply``.
* status-levels (read-only, fixed vocabulary) + settings CRUD.

Credential guard: PATCHing a dataset's ``script``/``method``/``credential_id``
while a credential is attached requires re-entering the secret. This prevents
a hijacked browser session from silently repurposing stored credentials.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import collector, db, query, scheduler, templates as tpl
from .crypto import secret_box
from .executor import ALL_METHODS

BASE_DIR = Path(__file__).resolve().parent.parent
# Static bundles, overridable for tests (SM_ADMIN_DIR / SM_DASHBOARD_DIR);
# data dir via SM_DATA_DIR.
#   /admin     — full app (protect behind webserver authentication)
#   /dashboard — read-only graph + status table
ADMIN_DIR = Path(os.environ.get("SM_ADMIN_DIR", str(BASE_DIR / "admin")))
DASHBOARD_DIR = Path(os.environ.get("SM_DASHBOARD_DIR", str(BASE_DIR / "dashboard")))
PREFIXES = ("/admin", "/dashboard")

# Network Resource types. Legacy resource values (vm/dns_alias/file_share) are
# remapped to these on startup by db.migrate_network_resources().
BASE_NETWORK_RESOURCES = ["server", "dns", "shared_folder", "web"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if os.environ.get("SM_DISABLE_SCHEDULER") != "1":
        scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="Uptracker", lifespan=lifespan)


@app.middleware("http")
async def prefix_api(request: Request, call_next):
    """Map /admin/api/... and /dashboard/api/... onto the shared /api routes.

    Each static bundle uses relative fetch URLs (anchored by a <base> tag), so
    the same API must be reachable under the bundle's own path prefix — which
    also keeps both bundles working behind a port-forwarding proxy."""
    path = request.scope["path"]
    for p in PREFIXES:
        if path == f"{p}/api" or path.startswith(f"{p}/api/"):
            request.scope["path"] = path[len(p):]
            break
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Force revalidation of frontend assets so edits aren't masked by stale
    browser caches (StaticFiles still serves 304s via ETag/Last-Modified)."""
    response = await call_next(request)
    path = request.url.path
    if (path == "/" or path.startswith(("/js/", "/vendor/", "/app.css"))
            or any(path.startswith(f"{p}/") for p in PREFIXES)):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    return db.connect()


def _levels_map(conn) -> dict[int, dict]:
    rows = conn.execute("SELECT id, name, color, rank, is_default FROM status_levels").fetchall()
    return {r["id"]: dict(r) for r in rows}


def _status_of(conn, level_id: int | None, levels: dict[int, dict]) -> dict:
    if level_id and level_id in levels:
        return {"name": levels[level_id]["name"], "color": levels[level_id]["color"]}
    return {"name": "unknown", "color": "#888888"}


def _value_dict(val: sqlite3.Row, levels: dict[int, dict]) -> dict:
    """Shape a metric_values row for the API, resolving its status once."""
    st = _status_of(None, val["status_level_id"], levels)
    return {
        "raw": val["raw"], "num": val["num"], "txt": val["txt"],
        "status": st["name"], "status_color": st["color"],
        "collected_at": val["collected_at"],
    }


def _require(conn, table: str, row_id, label: str = "resource") -> sqlite3.Row:
    """Fetch a row by id or raise 404. Centralises the existence-check idiom."""
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{label} not found")
    return row


def _dataset_dict(conn, ds: sqlite3.Row, levels: dict[int, dict]) -> dict:
    creds = None
    if ds["credential_id"]:
        c = conn.execute("SELECT id, name, username FROM credentials WHERE id=?", (ds["credential_id"],)).fetchone()
        if c:
            creds = {"id": c["id"], "name": c["name"], "username": c["username"]}
    metrics = []
    for m in conn.execute("SELECT * FROM metrics WHERE dataset_id=? ORDER BY id", (ds["id"],)).fetchall():
        val = conn.execute("SELECT * FROM metric_values WHERE metric_id=?", (m["id"],)).fetchone()
        metrics.append({
            "id": m["id"], "name": m["name"], "regex": m["regex"], "group_index": m["group_index"],
            "cast": m["cast"], "unit": m["unit"],
            "rules": [
                {"id": r["id"], "op": r["op"], "value": r["value"],
                 "status_level_id": r["status_level_id"], "priority": r["priority"]}
                for r in conn.execute("SELECT * FROM rules WHERE metric_id=? ORDER BY priority DESC, id", (m["id"],)).fetchall()
            ],
            "alert": _alert_dict(conn, m["id"]),
            "value": None if val is None else _value_dict(val, levels),
        })
    return {
        "id": ds["id"], "node_id": ds["node_id"], "name": ds["name"], "method": ds["method"],
        "script": ds["script"], "timeout_s": ds["timeout_s"], "interval_s": ds["interval_s"],
        "enabled": bool(ds["enabled"]), "next_run_at": ds["next_run_at"], "last_run_at": ds["last_run_at"],
        "last_status": ds["last_status"], "last_error": ds["last_error"],
        "credential": creds, "metrics": metrics,
    }


def _alert_dict(conn, metric_id: int) -> dict | None:
    a = conn.execute("SELECT * FROM alert_configs WHERE metric_id=?", (metric_id,)).fetchone()
    if not a:
        return None
    return {
        "id": a["id"], "enabled": bool(a["enabled"]), "recipients": a["recipients"],
        "subject": a["subject"], "body": a["body"], "cooldown_s": a["cooldown_s"],
        "only_on_change": bool(a["only_on_change"]), "last_status": a["last_status"], "last_sent_at": a["last_sent_at"],
    }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
@app.get("/api/snapshot")
def snapshot() -> dict:
    with _conn() as conn:
        levels = _levels_map(conn)
        graph = db.get_graph_json(conn)
        nodes = []
        for n in conn.execute("SELECT * FROM nodes ORDER BY label").fetchall():
            ds = [_dataset_dict(conn, d, levels)
                  for d in conn.execute("SELECT * FROM datasets WHERE node_id=? ORDER BY id", (n["id"],)).fetchall()]
            nodes.append({
                "id": n["id"], "label": n["label"], "network_resource": n["network_resource"], "network_path": n["network_path"],
                "tracking": bool(n["tracking"]),
                "status": _status_of(conn, n["status_level_id"], levels),
                "status_updated_at": n["status_updated_at"],
                "datasets": ds,
            })
        return {
            "graph": graph,
            "status_levels": [dict(r) for r in conn.execute("SELECT * FROM status_levels ORDER BY rank").fetchall()],
            "nodes": nodes,
            "poll_interval_s": int(db.get_setting(conn, "poll_interval_s", "15")),
            "theme": db.get_setting(conn, "theme", "light"),
        }


# ---------------------------------------------------------------------------
# Graph layout
# ---------------------------------------------------------------------------
class GraphBody(BaseModel):
    model: dict[str, Any]


@app.put("/api/graph")
def put_graph(body: GraphBody) -> dict:
    model = body.model
    if not isinstance(model.get("nodes"), list):
        raise HTTPException(400, "model.nodes must be a list")
    with _conn() as conn:
        db.set_graph_json(conn, model)
        db.sync_nodes(conn, model)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class NodePatch(BaseModel):
    network_resource: str | None = None
    network_path: str | None = None
    tracking: bool | None = None


@app.patch("/api/nodes/{node_id}")
def patch_node(node_id: str, body: NodePatch) -> dict:
    with _conn() as conn:
        _require(conn, "nodes", node_id, "node")
        fields, vals = [], []
        if body.network_resource is not None:
            fields.append("network_resource=?"); vals.append(body.network_resource)
        if body.network_path is not None:
            fields.append("network_path=?"); vals.append(body.network_path)
        if body.tracking is not None:
            fields.append("tracking=?"); vals.append(1 if body.tracking else 0)
        if fields:
            fields.append("updated_at=?"); vals.append(db.now_iso())
            conn.execute(f"UPDATE nodes SET {', '.join(fields)} WHERE id=?", (*vals, node_id))
        if body.tracking is not None:
            # Kick scheduling immediately when tracking is enabled.
            conn.execute("UPDATE datasets SET next_run_at=? WHERE node_id=? AND enabled=1", (db.now_iso(), node_id))
    return {"ok": True}


@app.post("/api/nodes/{node_id}/run")
def run_node_now(node_id: str, background_tasks: BackgroundTasks) -> dict:
    """Refresh every enabled dataset of a node right now.

    Pulls ``next_run_at`` forward to now (so the periodic scheduler stays in
    sync and its freshness indicators update), then runs the collection
    in-process via FastAPI background tasks. Running it here — rather than
    waiting for the scheduler's next tick — means refresh works even when the
    scheduler is disabled or paused in Settings. Each dataset collects with
    its own connection so a slow script never blocks the response."""
    with _conn() as conn:
        _require(conn, "nodes", node_id, "node")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM datasets WHERE node_id=? AND enabled=1 ORDER BY id",
            (node_id,),
        ).fetchall()]
        cur = conn.execute(
            "UPDATE datasets SET next_run_at=? WHERE node_id=? AND enabled=1",
            (db.now_iso(), node_id),
        )

    def _run() -> None:
        for did in ids:
            try:
                with db.db_conn() as c2:
                    collector.collect_dataset(c2, did)
            except Exception:  # noqa: BLE001 - one bad dataset must not abort the rest
                pass

    background_tasks.add_task(_run)
    return {"ok": True, "queued": cur.rowcount, "started": len(ids)}


@app.get("/api/network-resources")
def list_network_resources() -> dict:
    with _conn() as conn:
        resources = set(BASE_NETWORK_RESOURCES)
        for r in conn.execute("SELECT DISTINCT json_extract(payload,'$.network_resource') k FROM templates WHERE template_type='dataset'").fetchall():
            if r["k"]:
                resources.add(r["k"])
        for r in conn.execute("SELECT DISTINCT network_resource FROM nodes WHERE network_resource NOT IN ({})".format(",".join("?" * len(BASE_NETWORK_RESOURCES))), BASE_NETWORK_RESOURCES).fetchall():
            resources.add(r["network_resource"])
    return {"network_resources": sorted(resources)}


# ---------------------------------------------------------------------------
# Datasets (with credential guard)
# ---------------------------------------------------------------------------
class DatasetBody(BaseModel):
    name: str
    method: str = "bash"
    script: str = ""
    timeout_s: int = 30
    interval_s: int = 600
    enabled: bool = True
    credential_id: int | None = None
    secret: str | None = None          # re-entered secret (required on script change w/ credential)


def _validate_method(method: str) -> None:
    if method not in ALL_METHODS:
        raise HTTPException(400, f"method must be one of {sorted(ALL_METHODS)}")


@app.post("/api/nodes/{node_id}/datasets")
def create_dataset(node_id: str, body: DatasetBody) -> dict:
    _validate_method(body.method)
    with _conn() as conn:
        if not conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone():
            raise HTTPException(404, "node not found")
        cred_id = body.credential_id
        if cred_id and not conn.execute("SELECT id FROM credentials WHERE id=?", (cred_id,)).fetchone():
            raise HTTPException(400, "credential not found")
        if cred_id and not body.secret:
            raise HTTPException(428, "Precondition Required: entering a credential requires the secret to be (re)entered")
        if cred_id and body.secret:
            conn.execute("UPDATE credentials SET secret_enc=?, updated_at=? WHERE id=?",
                         (secret_box().encrypt(body.secret), db.now_iso(), cred_id))
        cur = conn.execute(
            """INSERT INTO datasets (node_id, name, method, script, timeout_s, interval_s, enabled, next_run_at, credential_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (node_id, body.name, body.method, body.script, body.timeout_s, body.interval_s,
             1 if body.enabled else 0, db.now_iso(), cred_id, db.now_iso(), db.now_iso()),
        )
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (cur.lastrowid,)).fetchone()
        return _dataset_dict(conn, ds, _levels_map(conn))


@app.patch("/api/datasets/{dataset_id}")
def patch_dataset(dataset_id: int, body: DatasetBody) -> dict:
    _validate_method(body.method)
    with _conn() as conn:
        old = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if not old:
            raise HTTPException(404, "dataset not found")
        cred_id = body.credential_id if body.credential_id is not None else old["credential_id"]
        script_changed = (body.script != old["script"]) or (body.method != old["method"]) or (cred_id != old["credential_id"])
        needs_secret = bool(cred_id) and script_changed
        if needs_secret and not body.secret:
            raise HTTPException(
                428,
                "Precondition Required: the retrieval script changed while a credential is attached — re-enter the secret",
            )
        if body.secret:
            if not cred_id:
                raise HTTPException(400, "secret provided without a credential")
            conn.execute("UPDATE credentials SET secret_enc=?, updated_at=? WHERE id=?",
                         (secret_box().encrypt(body.secret), db.now_iso(), cred_id))
        conn.execute(
            """UPDATE datasets SET name=?, method=?, script=?, timeout_s=?, interval_s=?, enabled=?,
                   credential_id=?, next_run_at=? WHERE id=?""",
            (body.name, body.method, body.script, body.timeout_s, body.interval_s,
             1 if body.enabled else 0, cred_id, db.now_iso(), dataset_id),
        )
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return _dataset_dict(conn, ds, _levels_map(conn))


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: int) -> dict:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "dataset not found")
    return {"ok": True}


class RunBody(BaseModel):
    pass


@app.post("/api/datasets/{dataset_id}/run")
def run_dataset_now(dataset_id: int) -> dict:
    with _conn() as conn:
        _require(conn, "datasets", dataset_id, "dataset")
        return collector.collect_dataset(conn, dataset_id)


class TestRunBody(BaseModel):
    method: str
    script: str
    timeout_s: int = 30
    credential_id: int | None = None
    secret: str | None = None
    metrics: list[dict] = []


@app.post("/api/test/run")
def test_run(body: TestRunBody) -> dict:
    """Dry-run a draft dataset definition; nothing is persisted."""
    _validate_method(body.method)
    from .executor import run_dataset as _run
    from .parser import extract
    secret, username = None, None
    if body.credential_id:
        with _conn() as conn:
            row = conn.execute("SELECT username, secret_enc FROM credentials WHERE id=?", (body.credential_id,)).fetchone()
            if not row:
                raise HTTPException(400, "credential not found")
            username = row["username"]
            secret = body.secret if body.secret else secret_box().decrypt(row["secret_enc"])
    result = _run(body.method, body.script, body.timeout_s, secret, username)
    parsed = []
    for m in body.metrics:
        v = extract(result.raw, m.get("regex", ""), m.get("group_index", 1), m.get("cast", "auto"))
        parsed.append({"name": m.get("name", ""), "raw": v.raw, "num": v.num, "txt": v.txt, "error": v.error})
    return {"ok": result.ok, "raw": result.raw[:20000], "error": result.error, "parsed": parsed}


# ---------------------------------------------------------------------------
# Metrics & rules
# ---------------------------------------------------------------------------
class MetricBody(BaseModel):
    name: str
    regex: str
    group_index: int = 1
    cast: str = "auto"
    unit: str = ""


@app.post("/api/datasets/{dataset_id}/metrics")
def create_metric(dataset_id: int, body: MetricBody) -> dict:
    with _conn() as conn:
        _require(conn, "datasets", dataset_id, "dataset")
        cur = conn.execute(
            "INSERT INTO metrics (dataset_id, name, regex, group_index, cast, unit, created_at) VALUES (?,?,?,?,?,?,?)",
            (dataset_id, body.name, body.regex, body.group_index, body.cast, body.unit, db.now_iso()),
        )
        return {"id": cur.lastrowid, "name": body.name}


@app.patch("/api/metrics/{metric_id}")
def patch_metric(metric_id: int, body: MetricBody) -> dict:
    with _conn() as conn:
        _require(conn, "metrics", metric_id, "metric")
        conn.execute(
            "UPDATE metrics SET name=?, regex=?, group_index=?, cast=?, unit=? WHERE id=?",
            (body.name, body.regex, body.group_index, body.cast, body.unit, metric_id),
        )
    return {"ok": True}


@app.delete("/api/metrics/{metric_id}")
def delete_metric(metric_id: int) -> dict:
    with _conn() as conn:
        if conn.execute("DELETE FROM metrics WHERE id=?", (metric_id,)).rowcount == 0:
            raise HTTPException(404, "metric not found")
    return {"ok": True}


class RuleBody(BaseModel):
    op: str
    value: str = ""
    status_level_id: int
    priority: int = 0


@app.post("/api/metrics/{metric_id}/rules")
def create_rule(metric_id: int, body: RuleBody) -> dict:
    from .rules import OPS
    if body.op not in OPS:
        raise HTTPException(400, f"op must be one of {OPS}")
    with _conn() as conn:
        _require(conn, "metrics", metric_id, "metric")
        if not conn.execute("SELECT id FROM status_levels WHERE id=?", (body.status_level_id,)).fetchone():
            raise HTTPException(400, "status level not found")
        cur = conn.execute(
            "INSERT INTO rules (metric_id, op, value, status_level_id, priority) VALUES (?,?,?,?,?)",
            (metric_id, body.op, body.value, body.status_level_id, body.priority),
        )
        collector.refresh_metric_status(conn, metric_id)
        return {"id": cur.lastrowid}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT metric_id FROM rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "rule not found")
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        collector.refresh_metric_status(conn, row["metric_id"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class AlertBody(BaseModel):
    enabled: bool = True
    recipients: str = ""
    subject: str = ""
    body: str = ""
    cooldown_s: int = 3600
    only_on_change: bool = True


@app.put("/api/metrics/{metric_id}/alert")
def upsert_alert(metric_id: int, body: AlertBody) -> dict:
    with _conn() as conn:
        _require(conn, "metrics", metric_id, "metric")
        conn.execute(
            """INSERT INTO alert_configs (metric_id, enabled, recipients, subject, body, cooldown_s, only_on_change)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(metric_id) DO UPDATE SET enabled=excluded.enabled, recipients=excluded.recipients,
                 subject=excluded.subject, body=excluded.body, cooldown_s=excluded.cooldown_s,
                 only_on_change=excluded.only_on_change""",
            (metric_id, 1 if body.enabled else 0, body.recipients, body.subject, body.body,
             body.cooldown_s, 1 if body.only_on_change else 0),
        )
        return _alert_dict(conn, metric_id)


@app.delete("/api/metrics/{metric_id}/alert")
def delete_alert(metric_id: int) -> dict:
    with _conn() as conn:
        conn.execute("DELETE FROM alert_configs WHERE metric_id=?", (metric_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Credentials (write-only secrets)
# ---------------------------------------------------------------------------
class CredentialBody(BaseModel):
    name: str
    username: str = ""
    secret: str


@app.post("/api/credentials")
def create_credential(body: CredentialBody) -> dict:
    if not body.secret:
        raise HTTPException(400, "secret is required")
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO credentials (name, username, secret_enc, created_at, updated_at) VALUES (?,?,?,?,?)",
                (body.name, body.username, secret_box().encrypt(body.secret), db.now_iso(), db.now_iso()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "credential name already exists")
        return {"id": cur.lastrowid, "name": body.name, "username": body.username}


@app.put("/api/credentials/{cred_id}")
def update_credential(cred_id: int, body: CredentialBody) -> dict:
    with _conn() as conn:
        _require(conn, "credentials", cred_id, "credential")
        enc = secret_box().encrypt(body.secret) if body.secret else conn.execute(
            "SELECT secret_enc FROM credentials WHERE id=?", (cred_id,)).fetchone()["secret_enc"]
        try:
            if body.secret:
                # Re-entering the secret re-encrypts it with this instance's key,
                # so the "needs re-key" flag (set by import) is cleared.
                conn.execute("UPDATE credentials SET name=?, username=?, secret_enc=?, needs_rekey=0, updated_at=? WHERE id=?",
                             (body.name, body.username, enc, db.now_iso(), cred_id))
            else:
                conn.execute("UPDATE credentials SET name=?, username=?, secret_enc=?, updated_at=? WHERE id=?",
                             (body.name, body.username, enc, db.now_iso(), cred_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "credential name already exists")
    return {"ok": True}


@app.get("/api/credentials")
def list_credentials() -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, username, needs_rekey, created_at, updated_at FROM credentials ORDER BY name"
        ).fetchall()
        return {"credentials": [dict(r) for r in rows]}


@app.delete("/api/credentials/{cred_id}")
def delete_credential(cred_id: int) -> dict:
    with _conn() as conn:
        if conn.execute("DELETE FROM credentials WHERE id=?", (cred_id,)).rowcount == 0:
            raise HTTPException(404, "credential not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
class TemplateBody(BaseModel):
    name: str
    template_type: str
    payload: dict


@app.get("/api/templates")
def list_templates(template_type: str | None = None) -> dict:
    with _conn() as conn:
        return {"templates": tpl.list_templates(conn, template_type)}


@app.post("/api/templates")
def save_template(body: TemplateBody) -> dict:
    if body.template_type not in ("dataset", "metric", "rule"):
        raise HTTPException(400, "template_type must be dataset|metric|rule")
    with _conn() as conn:
        return tpl.save_template(conn, body.name, body.template_type, body.payload)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int) -> dict:
    with _conn() as conn:
        if not tpl.delete_template(conn, template_id):
            raise HTTPException(404, "template not found")
    return {"ok": True}


class ApplyBody(BaseModel):
    node_id: str
    params: dict[str, str] = {}


@app.post("/api/templates/{template_id}/apply")
def apply_template(template_id: int, body: ApplyBody) -> dict:
    with _conn() as conn:
        try:
            return tpl.apply_dataset_template(conn, template_id, body.node_id, body.params)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# Read-only SQL queries + saved "Views"
# ---------------------------------------------------------------------------
# The admin query bar runs ad-hoc SQL; the dashboard picks a saved query. Both
# filter the bottom table's rows by node id. See app/query.py for the safety
# model (read-only connection + authorizer allowlist).
class QueryBody(BaseModel):
    sql: str


@app.get("/api/query/columns")
def query_columns() -> dict:
    """Column names available to a query (node fields + every metric name)."""
    return query.list_columns()


@app.post("/api/query")
def run_query(body: QueryBody) -> dict:
    try:
        return query.run_query(body.sql)
    except query.QueryError as exc:
        raise HTTPException(400, str(exc))


class SavedQueryBody(BaseModel):
    name: str
    sql: str


@app.get("/api/saved-queries")
def list_saved_queries() -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, sql FROM saved_queries ORDER BY name").fetchall()
        return {"saved_queries": [dict(r) for r in rows]}


@app.post("/api/saved-queries")
def save_saved_query(body: SavedQueryBody) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    # Validate by dry-running before persisting so a broken view is never saved.
    try:
        query.run_query(body.sql)
    except query.QueryError as exc:
        raise HTTPException(400, str(exc))
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO saved_queries (name, sql, created_at) VALUES (?, ?, ?)",
                (name, body.sql, db.now_iso()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"a saved query named {name!r} already exists")
        return {"id": cur.lastrowid, "name": name, "sql": body.sql}


@app.delete("/api/saved-queries/{saved_id}")
def delete_saved_query(saved_id: int) -> dict:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM saved_queries WHERE id=?", (saved_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "saved query not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Status levels & settings
# ---------------------------------------------------------------------------
# The status vocabulary is fixed (good/warning/critical/unknown) and seeded by
# db.py, so there is no create/delete API — rules pick from the list.
@app.get("/api/status-levels")
def list_levels() -> dict:
    with _conn() as conn:
        return {"status_levels": [dict(r) for r in conn.execute("SELECT * FROM status_levels ORDER BY rank").fetchall()]}


@app.get("/api/settings")
def get_settings() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {r["key"]: r["value"] for r in rows}
        # Never expose the SMTP password.
        out["smtp_password"] = "***" if out.get("smtp_password") else ""
        return out


class SettingsBody(BaseModel):
    values: dict[str, str]


@app.put("/api/settings")
def put_settings(body: SettingsBody) -> dict:
    with _conn() as conn:
        for k, v in body.values.items():
            if k == "smtp_password" and v == "***":
                continue
            db.set_setting(conn, k, v)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Import / Export (whole-instance archive)
# ---------------------------------------------------------------------------
# The archive is a zip of: monitor.db (a consistent snapshot via the SQLite
# backup API), graph.json (the nodegraph layout, for convenience/inspection),
# and manifest.json. The credential keyfile (secret.key) is deliberately NOT
# included, so credentials cannot be decrypted on another instance — those are
# flagged ``needs_rekey`` on import and highlighted in the Credentials tab.
REQUIRED_TABLES = {"settings", "status_levels", "graph_model", "nodes", "credentials"}


@app.get("/api/export")
def export_archive() -> Response:
    """Download the whole instance (database + graph) as a zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Consistent snapshot of the live DB (WAL-safe) into the archive.
        src = db.connect()
        try:
            tmp = tempfile.NamedTemporaryFile(prefix="sm-export-", suffix=".db", delete=False)
            tmp.close()
            try:
                dest = sqlite3.connect(tmp.name)
                try:
                    src.backup(dest)
                finally:
                    dest.close()
                zf.write(tmp.name, "monitor.db")
            finally:
                os.unlink(tmp.name)
            with _conn() as gconn:
                graph = db.get_graph_json(gconn)
                cred_count = gconn.execute("SELECT COUNT(*) c FROM credentials").fetchone()["c"]
        finally:
            src.close()
        zf.writestr("graph.json", json.dumps(graph))
        manifest = {
            "version": 1,
            "exported_at": db.now_iso(),
            "includes_secret_key": False,
            "credential_count": cred_count,
            "credentials_reencrypt_required": cred_count > 0,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    fname = f"uptracker-export-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class ImportBody(BaseModel):
    zip_b64: str


def _validate_sqlite(path: Path) -> None:
    """Raise 400 unless ``path`` is a SQLite DB containing our tables."""
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise HTTPException(400, f"archive database is not readable: {exc}")
    try:
        names = {r[0] for r in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        probe.close()
    missing = REQUIRED_TABLES - names
    if missing:
        raise HTTPException(400, "archive database is missing table(s): " + ", ".join(sorted(missing)))


@app.post("/api/import")
def import_archive(body: ImportBody) -> dict:
    """Replace the running instance from an exported zip archive.

    Backs up the current database first, then swaps in the archive's
    ``monitor.db``. Credentials that cannot be decrypted with this instance's
    keyfile are flagged ``needs_rekey`` and returned so the UI can prompt for
    them."""
    try:
        raw = base64.b64decode(body.zip_b64)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad upload
        raise HTTPException(400, "invalid base64 payload") from exc
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "upload is not a valid zip archive") from exc

    if "monitor.db" not in zf.namelist():
        raise HTTPException(400, "archive is missing monitor.db")

    db.ensure_dirs()
    # Stage the uploaded DB to a temp file and validate before touching live data.
    staged = tempfile.NamedTemporaryFile(dir=db.DATA_DIR, prefix="sm-import-", suffix=".db", delete=False)
    try:
        with zf.open("monitor.db") as src, open(staged.name, "wb") as out:
            out.write(src.read())
        staged.close()
        _validate_sqlite(Path(staged.name))

        # Back up the current DB (consistent snapshot) before replacing it.
        backup_name = db.DATA_DIR / f"monitor.pre-import-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
        cur = db.connect()
        try:
            bdest = sqlite3.connect(backup_name)
            try:
                cur.backup(bdest)
            finally:
                bdest.close()
        finally:
            cur.close()

        # Swap in the imported database and clear any stale WAL sidecars.
        os.replace(staged.name, db.DB_PATH)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db.DB_PATH) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    finally:
        if os.path.exists(staged.name):
            os.unlink(staged.name)

    # Bring the new DB up to the current schema, then re-apply the graph.json
    # copy (authoritative layout) and reconcile node rows.
    db.init_db()
    if "graph.json" in zf.namelist():
        try:
            model = json.loads(zf.read("graph.json").decode("utf-8"))
            if isinstance(model, dict) and isinstance(model.get("nodes"), list):
                with _conn() as conn:
                    db.set_graph_json(conn, model)
                    db.sync_nodes(conn, model)
        except (ValueError, UnicodeDecodeError):
            pass  # graph.json is a convenience copy; the DB is authoritative

    # Flag credentials whose secret can't be decrypted with this keyfile.
    flagged: list[dict] = []
    with _conn() as conn:
        for c in conn.execute("SELECT id, name, username, secret_enc FROM credentials").fetchall():
            try:
                secret_box().decrypt(c["secret_enc"])
                conn.execute("UPDATE credentials SET needs_rekey=0 WHERE id=?", (c["id"],))
            except ValueError:
                conn.execute("UPDATE credentials SET needs_rekey=1 WHERE id=?", (c["id"],))
                flagged.append({"id": c["id"], "name": c["name"], "username": c["username"]})
        counts = {
            "nodes": conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"],
            "datasets": conn.execute("SELECT COUNT(*) c FROM datasets").fetchone()["c"],
        }
    return {"ok": True, "credentials_needing_rekey": flagged, **counts}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Landing: the read-only dashboard. The full editor lives under /admin."""
    return RedirectResponse("/dashboard/", status_code=307)


for _name, _dir in (("admin", ADMIN_DIR), ("dashboard", DASHBOARD_DIR)):
    (_dir / "vendor").mkdir(parents=True, exist_ok=True)
    app.mount(f"/{_name}", StaticFiles(directory=_dir, html=True), name=_name)
