"""SQLite persistence layer: connection management, schema, and migrations.

The database is the single source of truth for all application state. The
nodegraph layout (nodes/edges/groups positions) is stored verbatim as JSON in
the ``graph_model`` row because nodegraph's importJSON strips unknown fields,
so we cannot smuggle app data through the graph model itself. Domain data
(datasets, metrics, rules, credentials, latest values) lives in normalised
tables keyed by the nodegraph node id.

Only the most recent value per metric is kept (``metric_values`` has one row
per metric, replaced on every collection). No historical time series.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Paths (SM_DATA_DIR overrides for tests)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SM_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "monitor.db"
KEY_PATH = DATA_DIR / "secret.key"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    """Open a connection with sensible defaults (WAL, FK enforcement, Row)."""
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Fixed status vocabulary (good/warning/critical/unknown). Not user-editable:
-- rules assign good/warning/critical; unknown is the fallback. rank orders
-- severity (higher = worse) so a node's overall status is the worst rank
-- across its tracked datasets.
CREATE TABLE IF NOT EXISTS status_levels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT NOT NULL DEFAULT '#888888',
    rank       INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0
);

-- Authoritative nodegraph layout (the editor's exportJSON output).
CREATE TABLE IF NOT EXISTS graph_model (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    json TEXT NOT NULL
);

-- One row per tracked network object; id matches the nodegraph node id.
CREATE TABLE IF NOT EXISTS nodes (
    id                TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    network_path      TEXT NOT NULL DEFAULT '',
    tags              TEXT NOT NULL DEFAULT '',
    status_level_id   INTEGER REFERENCES status_levels(id) ON DELETE SET NULL,
    status_updated_at TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- A retrieval job attached to a node.
CREATE TABLE IF NOT EXISTS datasets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id        TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    method         TEXT NOT NULL DEFAULT 'bash',   -- bash|cmd|powershell
    script         TEXT NOT NULL DEFAULT '',        -- shell command body
    timeout_s      INTEGER NOT NULL DEFAULT 30,
    interval_s     INTEGER NOT NULL DEFAULT 600,
    enabled        INTEGER NOT NULL DEFAULT 1,
    next_run_at    TEXT,
    last_run_at    TEXT,
    last_status    TEXT,                            -- 'ok'|'error'|'timeout'|...
    last_error     TEXT,
    credential_id  INTEGER REFERENCES credentials(id) ON DELETE SET NULL,
    param_values   TEXT NOT NULL DEFAULT '{}',      -- JSON: [Param] token -> value (script keeps tokens)
    last_output    TEXT,                            -- full stdout/stderr of the latest run (for regex building)
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- A named variable extracted from a dataset's raw output via regex capture.
CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id   INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    regex        TEXT NOT NULL,
    group_index  INTEGER NOT NULL DEFAULT 1,
    cast         TEXT NOT NULL DEFAULT 'auto',      -- auto|int|float|str|bool
    unit         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

-- A rule maps a metric value to a status level.
CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id       INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    op              TEXT NOT NULL,                  -- gt|lt|gte|lte|eq|ne|contains|regex|missing|present
    value           TEXT NOT NULL DEFAULT '',
    status_level_id INTEGER NOT NULL REFERENCES status_levels(id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 0
);

-- Optional email alerting per metric (a ruleset trigger).
CREATE TABLE IF NOT EXISTS alert_configs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id      INTEGER NOT NULL UNIQUE REFERENCES metrics(id) ON DELETE CASCADE,
    enabled        INTEGER NOT NULL DEFAULT 0,
    recipients     TEXT NOT NULL DEFAULT '',         -- comma separated
    subject        TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    cooldown_s     INTEGER NOT NULL DEFAULT 3600,
    only_on_change INTEGER NOT NULL DEFAULT 1,
    trigger_count  INTEGER NOT NULL DEFAULT 1,      -- consecutive scheduled qualifying runs required before emailing
    trigger_streak INTEGER NOT NULL DEFAULT 0,      -- runtime counter of consecutive scheduled qualifying runs
    trigger_statuses TEXT NOT NULL DEFAULT '["warning","critical"]',  -- JSON array of status names that qualify the streak
    last_status    TEXT,                             -- status when last alerted
    last_sent_at   TEXT
);

-- Encrypted credentials. secret_enc is Fernet ciphertext; never returned raw.
-- needs_rekey is set on import when a stored secret cannot be decrypted with
-- this instance's keyfile (the export archive deliberately omits the key), so
-- the operator knows to re-enter it.
CREATE TABLE IF NOT EXISTS credentials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    username    TEXT NOT NULL DEFAULT '',
    secret_enc  TEXT NOT NULL,
    needs_rekey INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Latest collected value per metric (replaced each run; no history).
CREATE TABLE IF NOT EXISTS metric_values (
    metric_id       INTEGER PRIMARY KEY REFERENCES metrics(id) ON DELETE CASCADE,
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    raw             TEXT,
    num             REAL,
    txt             TEXT,
    status_level_id INTEGER REFERENCES status_levels(id) ON DELETE SET NULL,
    collected_at    TEXT NOT NULL
);

-- Reusable templates for datasets, metrics, and rules.
CREATE TABLE IF NOT EXISTS templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    template_type TEXT NOT NULL,                     -- dataset|metric|rule
    payload       TEXT NOT NULL,                     -- JSON
    created_at    TEXT NOT NULL
);

-- Named read-only SQL queries ("Views") for the bottom nodes table. The admin
-- query bar saves them; the dashboard lists them in a dropdown. ``sql`` must be
-- a single SELECT returning node ids in its first column (see app/query.py).
CREATE TABLE IF NOT EXISTS saved_queries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sql        TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_datasets_node     ON datasets(node_id);
CREATE INDEX IF NOT EXISTS idx_datasets_next      ON datasets(next_run_at);
CREATE INDEX IF NOT EXISTS idx_metrics_dataset    ON metrics(dataset_id);
CREATE INDEX IF NOT EXISTS idx_rules_metric       ON rules(metric_id);
CREATE INDEX IF NOT EXISTS idx_values_dataset     ON metric_values(dataset_id);
-- The query pivot (app/query.py ``_build_wide``) resolves metrics by case-insensitive
-- name; without this expression index every column lookup scans the whole metrics
-- table. (Values/alerts need no extra index: ``metric_values.metric_id`` is the PK
-- and ``alert_configs.metric_id`` is UNIQUE, both already indexed.)
CREATE INDEX IF NOT EXISTS idx_metrics_name       ON metrics(name COLLATE NOCASE);
"""


def init_db() -> None:
    """Create tables and seed defaults. Idempotent."""
    with db_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_credentials_rekey(conn)
        migrate_rename_columns(conn)
        migrate_status_levels(conn)
        _seed_status_levels(conn)
        _seed_settings(conn)
        migrate_mustache_tokens(conn)
        migrate_dataset_param_values(conn)
        migrate_strip_known_params(conn)
        migrate_dataset_last_output(conn)
        migrate_alert_trigger_count(conn)
        migrate_rename_network_path_token(conn)
        migrate_drop_node_tracking(conn)
        migrate_drop_network_resource_and_clear_templates(conn)
        migrate_add_node_tags(conn)
        row = conn.execute("SELECT json FROM graph_model WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO graph_model (id, json) VALUES (1, ?)",
                [json.dumps({"version": 1, "nodes": [], "edges": [], "groups": []})],
            )
        _seed_default_templates(conn)


def _migrate_credentials_rekey(conn: sqlite3.Connection) -> None:
    """Add ``credentials.needs_rekey`` to databases created before the column
    existed. ``CREATE TABLE IF NOT EXISTS`` leaves old tables untouched, so the
    column has to be added explicitly. Idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(credentials)")}
    if "needs_rekey" not in cols:
        conn.execute("ALTER TABLE credentials ADD COLUMN needs_rekey INTEGER NOT NULL DEFAULT 0")


def migrate_dataset_param_values(conn: sqlite3.Connection) -> None:
    """Add ``datasets.param_values`` (JSON map of ``[Param]`` token -> value) to
    databases created before the column existed. The dataset ``script`` now keeps
    its ``[Param]`` tokens and they are resolved at execution time from this map,
    so values persist across reloads instead of being baked into the script.
    Idempotent — a no-op once the column exists."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)")}
    if "param_values" not in cols:
        conn.execute("ALTER TABLE datasets ADD COLUMN param_values TEXT NOT NULL DEFAULT '{}'")


# Node-derived [Param] names — mirrored from app.templates.KNOWN_PARAM_KEYS.
# Duplicated here (rather than imported) because templates.py imports this
# module, so importing back would be circular.
_KNOWN_PARAM_KEYS = {"label", "id", "network path"}


def migrate_strip_known_params(conn: sqlite3.Connection) -> None:
    """Remove node-derived keys from stored dataset ``param_values``.

    Older versions of the admin persisted every parameter field — including the
    node-derived ones (``[label]``, ``[id]``, ``[Network Path]``, …) —
    into a dataset's ``param_values`` blob. Those saved copies shadowed the live
    node fields at execution time, so a renamed node or changed network path
    kept using the stale value baked in at save time (e.g. ``ping [label]``
    resolving to an old name). Node-derived params always resolve from the node
    itself now, so any such key is dropped from existing rows. Custom tokens are
    preserved untouched. Idempotent: rows with nothing to strip are skipped."""
    try:
        rows = conn.execute("SELECT id, param_values FROM datasets").fetchall()
    except sqlite3.OperationalError:
        return  # datasets table absent (fresh db before schema) — nothing to do
    for r in rows:
        try:
            vals = json.loads(r["param_values"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(vals, dict):
            continue
        kept = {k: v for k, v in vals.items() if str(k).strip().lower() not in _KNOWN_PARAM_KEYS}
        if len(kept) != len(vals):
            conn.execute("UPDATE datasets SET param_values=? WHERE id=?",
                         [json.dumps(kept), r["id"]])


def migrate_dataset_last_output(conn: sqlite3.Connection) -> None:
    """Add ``datasets.last_output`` (the full stdout/stderr of the most recent
    collection run) to databases created before the column existed. The admin
    metrics page shows this raw output so a regex can be built against it.
    Idempotent — a no-op once the column exists."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)")}
    if "last_output" not in cols:
        conn.execute("ALTER TABLE datasets ADD COLUMN last_output TEXT")


# ``[Network-Path Reference]`` was renamed to ``[Network Path]``. Rewrite stored
# scripts, alert subjects/bodies, and template payloads (and rename the matching
# key inside saved ``param_values`` blobs) so existing data keeps resolving.
_NETWORK_PATH_TOKEN_OLD = "[Network-Path Reference]"
_NETWORK_PATH_TOKEN_NEW = "[Network Path]"


def migrate_rename_network_path_token(conn: sqlite3.Connection) -> None:
    """Rename the ``[Network-Path Reference]`` parameter token to
    ``[Network Path]`` across stored dataset scripts, alert subjects/bodies,
    and template payloads. Also rename the matching lower-cased key inside any
    saved ``datasets.param_values`` JSON blob. Idempotent — rows without the
    old token/key are skipped."""
    for table, cols in (("datasets", ("script",)),
                        ("alert_configs", ("subject", "body"))):
        for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
            updates = {c: (row[c] or "").replace(_NETWORK_PATH_TOKEN_OLD, _NETWORK_PATH_TOKEN_NEW) for c in cols}
            if any(updates[c] != row[c] for c in cols):
                sets = ", ".join(f"{c}=?" for c in cols)
                conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                             (*[updates[c] for c in cols], row["id"]))
    for row in conn.execute("SELECT id, payload FROM templates").fetchall():
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        new_payload = _rewrite_text_tree(payload, _NETWORK_PATH_TOKEN_OLD, _NETWORK_PATH_TOKEN_NEW)
        if new_payload != payload:
            conn.execute("UPDATE templates SET payload=? WHERE id=?",
                         [json.dumps(new_payload), row["id"]])
    for row in conn.execute("SELECT id, param_values FROM datasets").fetchall():
        try:
            vals = json.loads(row["param_values"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(vals, dict):
            continue
        renamed: dict = {}
        changed = False
        for k, v in vals.items():
            new_k = _NETWORK_PATH_TOKEN_NEW.strip("[]") if str(k).strip().lower() == "network-path reference" else k
            if new_k != k:
                changed = True
            renamed[new_k] = v
        if changed:
            conn.execute("UPDATE datasets SET param_values=? WHERE id=?",
                         [json.dumps(renamed), row["id"]])


def _rewrite_text_tree(obj, old: str, new: str):
    """Recursively replace ``old`` with ``new`` inside every string of a JSON
    value. Returns the (possibly new) object."""
    if isinstance(obj, dict):
        return {k: _rewrite_text_tree(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_text_tree(v, old, new) for v in obj]
    if isinstance(obj, str):
        return obj.replace(old, new)
    return obj


def migrate_drop_node_tracking(conn: sqlite3.Connection) -> None:
    """Drop the legacy ``nodes.tracking`` column. It was a per-node opt-in flag
    that only kicked scheduling on PATCH; the global Scheduler setting plus each
    dataset's own ``enabled`` field cover the same need, so the column is now
    unused. Idempotent — no-op if already dropped."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "tracking" in cols:
        conn.execute("ALTER TABLE nodes DROP COLUMN tracking")


def migrate_add_node_tags(conn: sqlite3.Connection) -> None:
    """Add ``nodes.tags`` (comma-separated list of free-form labels) to databases
    created before the column existed. Tags are editable on the node panel and
    exposed in the bottom tables / query layer for filtering. Idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "tags" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN tags TEXT NOT NULL DEFAULT ''")


# ``[Network Resource]`` parameter token and its lower-cased param_values key,
# plus the legacy ``{{node.network_resource}}`` mustache reference. All removed.
_NETWORK_RESOURCE_TOKEN = "[Network Resource]"
_NETWORK_RESOURCE_PARAM_KEY = "network resource"
_NETWORK_RESOURCE_MUSTACHE_RE = re.compile(r"\{\{\s*node\.network_resource\s*\}\}")


def _strip_network_resource_text(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    out = text.replace(_NETWORK_RESOURCE_TOKEN, "")
    return _NETWORK_RESOURCE_MUSTACHE_RE.sub("", out)


def _strip_network_resource_tree(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_network_resource_tree(v) for k, v in obj.items()
                if str(k).strip().lower() != _NETWORK_RESOURCE_PARAM_KEY}
    if isinstance(obj, list):
        return [_strip_network_resource_tree(v) for v in obj]
    if isinstance(obj, str):
        return _strip_network_resource_text(obj)
    return obj


def migrate_drop_network_resource_and_clear_templates(conn: sqlite3.Connection) -> None:
    """Fully remove the "Network Resource" concept.

    Bundled and user-saved dataset templates (``templates.template_type='dataset'``)
    are deleted because their payloads carry a ``network_resource`` field that no
    longer maps to anything in the schema or UI. Stored dataset scripts, alert
    subjects/bodies, and remaining template payloads have any ``[Network Resource]``
    token and ``{{node.network_resource}}`` mustache reference stripped; saved
    ``datasets.param_values`` blobs lose the matching lower-cased key. Finally the
    legacy ``nodes.network_resource`` column is dropped. Idempotent — a no-op once
    the column is gone."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "network_resource" not in cols:
        return

    conn.execute("DELETE FROM templates WHERE template_type='dataset'")

    for table, text_cols in (("datasets", ("script",)),
                             ("alert_configs", ("subject", "body"))):
        for row in conn.execute(f"SELECT id, {', '.join(text_cols)} FROM {table}").fetchall():
            updates = {c: _strip_network_resource_text(row[c]) for c in text_cols}
            if any(updates[c] != row[c] for c in text_cols):
                sets = ", ".join(f"{c}=?" for c in text_cols)
                conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                             (*[updates[c] for c in text_cols], row["id"]))

    for row in conn.execute("SELECT id, payload FROM templates").fetchall():
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        new_payload = _strip_network_resource_tree(payload)
        if new_payload != payload:
            conn.execute("UPDATE templates SET payload=? WHERE id=?",
                         [json.dumps(new_payload), row["id"]])

    for row in conn.execute("SELECT id, param_values FROM datasets").fetchall():
        try:
            vals = json.loads(row["param_values"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(vals, dict):
            continue
        kept = {k: v for k, v in vals.items()
                if str(k).strip().lower() != _NETWORK_RESOURCE_PARAM_KEY}
        if len(kept) != len(vals):
            conn.execute("UPDATE datasets SET param_values=? WHERE id=?",
                         [json.dumps(kept), row["id"]])

    conn.execute("ALTER TABLE nodes DROP COLUMN network_resource")


def migrate_alert_trigger_count(conn: sqlite3.Connection) -> None:
    """Add ``alert_configs.trigger_count`` (how many consecutive scheduled runs in a
    qualifying status are required before an email is sent; 1 = alert on the first
    qualifying run, the historical behaviour), ``trigger_streak`` (the runtime counter
    of consecutive scheduled qualifying runs), and ``trigger_statuses`` (a JSON array of
    status names that qualify the streak to increment) to databases created before these
    columns existed. On-demand runs neither increment nor reset the streak, so a transient
    blip caught by a manual run cannot trigger an email. Idempotent — a no-op once the
    columns exist."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alert_configs)")}
    if "trigger_count" not in cols:
        conn.execute("ALTER TABLE alert_configs ADD COLUMN trigger_count INTEGER NOT NULL DEFAULT 1")
    if "trigger_streak" not in cols:
        conn.execute("ALTER TABLE alert_configs ADD COLUMN trigger_streak INTEGER NOT NULL DEFAULT 0")
    if "trigger_statuses" not in cols:
        conn.execute(
            "ALTER TABLE alert_configs ADD COLUMN trigger_statuses TEXT NOT NULL DEFAULT '[\"warning\",\"critical\"]'"
        )


def migrate_rename_columns(conn: sqlite3.Connection) -> None:
    """Rename the legacy ``nodes.uri`` and ``templates.kind`` columns to
    ``network_path``/``template_type`` so the schema matches the UI terminology.
    ``CREATE TABLE IF NOT EXISTS`` leaves an existing (old) database untouched,
    so the rename has to be explicit. Idempotent — a no-op once the columns
    already carry the new names.

    Runs before ``migrate_mustache_tokens`` so that migration can assume the
    new column names. The legacy ``nodes.kind`` column is left in place when
    present; ``migrate_drop_network_resource_and_clear_templates`` drops it."""
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "uri" in node_cols and "network_path" not in node_cols:
        conn.execute("ALTER TABLE nodes RENAME COLUMN uri TO network_path")
    tpl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(templates)")}
    if "kind" in tpl_cols and "template_type" not in tpl_cols:
        conn.execute("ALTER TABLE templates RENAME COLUMN kind TO template_type")


# ``{{node.uri}}`` / ``{{node.kind}}`` mustache tokens rewritten to the new
# context keys. Applied to stored scripts, alert bodies, and template payloads.
_MUSTACHE_TOKENS = {
    "node.uri": "node.network_path",
    "node.kind": "node.network_resource",
}


def _rewrite_mustache(text: str) -> str:
    """Replace ``{{node.uri}}``/``{{node.kind}}`` (whitespace-tolerant) with the
    renamed context keys. Idempotent — the new names are not re-matched."""
    if not isinstance(text, str) or "{{" not in text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        path = m.group(1).strip()
        return "{{" + _MUSTACHE_TOKENS[path] + "}}" if path in _MUSTACHE_TOKENS else m.group(0)

    return re.sub(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", _sub, text)


def _rewrite_payload(obj: Any) -> Any:
    """Recursively rewrite mustache tokens in every string of a template payload,
    and rename the payload's legacy ``kind`` key (the node resource type) to
    ``network_resource``. Returns the (possibly new) object."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            new_key = "network_resource" if k == "kind" else k
            out[new_key] = _rewrite_payload(v)
        return out
    if isinstance(obj, list):
        return [_rewrite_payload(v) for v in obj]
    if isinstance(obj, str):
        return _rewrite_mustache(obj)
    return obj


def migrate_mustache_tokens(conn: sqlite3.Connection) -> None:
    """Rewrite stored ``{{node.uri}}``/``{{node.kind}}`` tokens to
    ``{{node.network_path}}``/``{{node.network_resource}}`` across dataset
    scripts, alert subjects/bodies, and template payloads (also renaming a
    dataset payload's ``kind`` key). Idempotent — a no-op once rewritten."""
    for table, cols in (("datasets", ("script",)),
                        ("alert_configs", ("subject", "body"))):
        for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
            updates = {c: _rewrite_mustache(row[c]) for c in cols}
            if any(updates[c] != row[c] for c in cols):
                sets = ", ".join(f"{c}=?" for c in cols)
                conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                             (*[updates[c] for c in cols], row["id"]))
    for row in conn.execute("SELECT id, payload FROM templates").fetchall():
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        new_payload = _rewrite_payload(payload)
        if new_payload != payload:
            conn.execute("UPDATE templates SET payload=? WHERE id=?",
                         [json.dumps(new_payload), row["id"]])


# The fixed status vocabulary. Statuses are not user-editable: rules assign
# good/warning/critical, and "unknown" is the fallback when no rule matches or
# no value could be retrieved. rank orders severity (higher = worse) so a
# node's overall status is the worst rank across its tracked datasets.
STATUS_LEVELS_FIXED = [
    ("good", "#2e9e44", 10),
    ("warning", "#e08a00", 20),
    ("critical", "#d33a3a", 30),
    ("unknown", "#888888", 0),
]

# Legacy user-editable names folded into the fixed vocabulary.
_STATUS_LEVEL_RENAME = {"up": "good", "degraded": "warning", "down": "critical"}


def migrate_status_levels(conn: sqlite3.Connection) -> None:
    """Fold pre-existing custom levels into the fixed vocabulary in place (by
    renaming, so rule/node foreign keys survive) and normalise the colors and
    ranks of the fixed names. Idempotent."""
    for old, new in _STATUS_LEVEL_RENAME.items():
        conn.execute("UPDATE status_levels SET name=? WHERE name=?", (new, old))
    for name, color, rank in STATUS_LEVELS_FIXED:
        conn.execute(
            "UPDATE status_levels SET color=?, rank=?, is_default=0 WHERE name=?",
            (color, rank, name),
        )


def _seed_status_levels(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM status_levels").fetchone()["c"]:
        return
    conn.executemany(
        "INSERT INTO status_levels (name, color, rank, is_default) VALUES (?,?,?,0)",
        STATUS_LEVELS_FIXED,
    )


def _seed_settings(conn: sqlite3.Connection) -> None:
    defaults = {
        "poll_interval_s": "15",
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_password": "",
        "smtp_starttls": "1",
        "mail_from": "uptracker@localhost",
        "scheduler_enabled": "1",
        "theme": "light",
        # Global defaults applied to new email alerts (and to alerts whose
        # subject/body are left blank at send time). INSERT OR IGNORE, so
        # existing databases pick these up on the next init_db.
        "alert_default_recipients": "",
        "alert_default_subject": "{{status}}: {{node.label}} / {{metric.name}}",
        "alert_default_body": "{{node.label}} — metric {{metric.name}} is {{status}} (value={{value}}).",
        "alert_default_cooldown_s": "3600",
        "alert_default_trigger_count": "1",
        "alert_default_trigger_statuses": '["warning","critical"]',
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v)
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_graph_json(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT json FROM graph_model WHERE id=1").fetchone()
    return json.loads(row["json"]) if row else {"version": 1, "nodes": [], "edges": [], "groups": []}


def set_graph_json(conn: sqlite3.Connection, model: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO graph_model (id, json) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET json=excluded.json",
        (json.dumps(model),),
    )


def sync_nodes(conn: sqlite3.Connection, model: dict[str, Any]) -> None:
    """Reconcile the ``nodes`` table against a nodegraph layout model.

    Creates rows for new nodes, updates labels (the editor is authoritative),
    and prunes nodes removed from the canvas (cascading to datasets/metrics/…).
    Shared by ``PUT /api/graph`` and the archive import so both paths stay in
    lockstep. Set-based (one statement per phase) instead of per-node queries,
    so large graphs save in constant time."""
    labels = {n["id"]: n.get("label", "Node") for n in model.get("nodes", []) if "id" in n}
    now = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO nodes (id, label, created_at, updated_at) VALUES (?,?,?,?)",
        [(nid, label, now, now) for nid, label in labels.items()],
    )
    conn.executemany(
        "UPDATE nodes SET label=?, updated_at=? WHERE id=? AND label!=?",
        [(label, now, nid, label) for nid, label in labels.items()],
    )
    model_ids = set(labels)
    if model_ids:
        ids = list(model_ids)
        for i in range(0, len(ids), 500):  # stay under SQLite's bind-parameter limit
            chunk = ids[i:i + 500]
            conn.execute(
                f"DELETE FROM nodes WHERE id NOT IN ({','.join('?' * len(chunk))})",
                chunk,
            )
    else:
        conn.execute("DELETE FROM nodes")


# ---------------------------------------------------------------------------
# Bundled default templates seed
# ---------------------------------------------------------------------------
def _seed_default_templates(conn: sqlite3.Connection) -> None:
    """Insert the bundled dataset templates (``templates.DEFAULT_TEMPLATES``)
    into a database that has never been seeded with them.

    A one-time marker setting (``default_templates_seeded``) makes this run
    exactly once per database: after the first seed, deletions stick across
    restarts and user edits are never overwritten. Rule ``level`` names in the
    payloads are resolved to ``status_levels`` ids at seed time (never
    hardcoded); unknown level names drop that rule."""
    from . import templates as tpl_mod

    if get_setting(conn, "default_templates_seeded") == "1":
        return
    levels = {
        r["name"]: r["id"]
        for r in conn.execute("SELECT id, name FROM status_levels").fetchall()
    }
    now = now_iso()
    for spec in tpl_mod.DEFAULT_TEMPLATES:
        payload = _resolve_template_levels(spec["payload"], levels)
        conn.execute(
            """INSERT INTO templates (name, template_type, payload, created_at) VALUES (?,?,?,?)
               ON CONFLICT(name) DO NOTHING""",
            (spec["name"], "dataset", json.dumps(payload), now),
        )
    set_setting(conn, "default_templates_seeded", "1")


def _resolve_template_levels(payload: dict, levels: dict) -> dict:
    """Return a deep copy of *payload* with each rule's ``level`` name replaced
    by the matching ``status_level_id``. Rules whose level name is unknown are
    dropped."""
    out = dict(payload)
    metrics = []
    for m in payload.get("metrics", []):
        mm = dict(m)
        rules = []
        for r in m.get("rules", []):
            rr = {k: v for k, v in r.items() if k != "level"}
            level_id = levels.get(r.get("level"))
            if level_id is None:
                continue
            rr["status_level_id"] = level_id
            rules.append(rr)
        mm["rules"] = rules
        metrics.append(mm)
    out["metrics"] = metrics
    return out

