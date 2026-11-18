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
    network_resource  TEXT NOT NULL DEFAULT 'server',
    network_path      TEXT NOT NULL DEFAULT '',
    tracking          INTEGER NOT NULL DEFAULT 0,
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
        migrate_network_resources(conn)
        migrate_mustache_tokens(conn)
        migrate_dataset_param_values(conn)
        migrate_dataset_last_output(conn)
        row = conn.execute("SELECT json FROM graph_model WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO graph_model (id, json) VALUES (1, ?)",
                [json.dumps({"version": 1, "nodes": [], "edges": [], "groups": []})],
            )
        _seed_demo(conn)


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


def migrate_dataset_last_output(conn: sqlite3.Connection) -> None:
    """Add ``datasets.last_output`` (the full stdout/stderr of the most recent
    collection run) to databases created before the column existed. The admin
    metrics page shows this raw output so a regex can be built against it.
    Idempotent — a no-op once the column exists."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)")}
    if "last_output" not in cols:
        conn.execute("ALTER TABLE datasets ADD COLUMN last_output TEXT")


# Legacy node/template network-resource values remapped onto the current types.
NETWORK_RESOURCE_REMAP = {"dns_alias": "dns", "file_share": "shared_folder", "vm": "server"}


def migrate_rename_columns(conn: sqlite3.Connection) -> None:
    """Rename the legacy ``nodes.kind``/``nodes.uri`` and ``templates.kind``
    columns to ``network_resource``/``network_path``/``template_type`` so the
    schema matches the UI terminology. ``CREATE TABLE IF NOT EXISTS`` leaves an
    existing (old) database untouched, so the rename has to be explicit.
    Idempotent — a no-op once the columns already carry the new names.

    Runs before ``migrate_network_resources``/``migrate_mustache_tokens`` so
    those can assume the new column names."""
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "kind" in node_cols and "network_resource" not in node_cols:
        conn.execute("ALTER TABLE nodes RENAME COLUMN kind TO network_resource")
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


def migrate_network_resources(conn: sqlite3.Connection) -> None:
    """Remap legacy network-resource values to the current types.

    Covers both ``nodes.network_resource`` and the ``network_resource`` field
    inside dataset-template payloads (``templates.payload``), which otherwise
    resurface in the resource list served to the UI. Idempotent — a no-op once
    everything is remapped."""
    for old, new in NETWORK_RESOURCE_REMAP.items():
        conn.execute("UPDATE nodes SET network_resource=? WHERE network_resource=?", (new, old))
    for r in conn.execute("SELECT id, payload FROM templates").fetchall():
        try:
            payload = json.loads(r["payload"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        new = NETWORK_RESOURCE_REMAP.get(payload.get("network_resource"))
        if new:
            payload["network_resource"] = new
            conn.execute(
                "UPDATE templates SET payload=? WHERE id=?",
                [json.dumps(payload), r["id"]],
            )


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
    lockstep."""
    model_ids = {n["id"] for n in model.get("nodes", []) if "id" in n}
    labels = {n["id"]: n.get("label", "Node") for n in model.get("nodes", []) if "id" in n}
    for nid in model_ids:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, label, created_at, updated_at) VALUES (?,?,?,?)",
            (nid, labels.get(nid, "Node"), now_iso(), now_iso()),
        )
    for nid, label in labels.items():
        conn.execute("UPDATE nodes SET label=?, updated_at=? WHERE id=? AND label!=?",
                     (label, now_iso(), nid, label))
    for row in conn.execute("SELECT id FROM nodes").fetchall():
        if row["id"] not in model_ids:
            conn.execute("DELETE FROM nodes WHERE id=?", (row["id"],))


# ---------------------------------------------------------------------------
# Demo seed (localhost node with system metrics + rules)
# ---------------------------------------------------------------------------
# One entry per dataset. Each dataset is a bash script run on the server host;
# its ``metrics`` extract named values via regex and attach status rules. Rule
# ``level`` names are resolved to status_level ids at seed time (never
# hardcoded). Evaluation is worst-wins, so a ``present``->good rule coexists
# safely with numeric ``gt``->warning/critical rules on the same metric.
DEMO_DATASETS: list[dict[str, Any]] = [
    {
        # One command, several labelled metrics: OS / kernel / hostname.
        "name": "system info",
        "script": (
            'grep ^PRETTY_NAME= /etc/os-release | cut -d\'"\' -f2 | sed \'s/^/os:/\'; '
            'printf \'kernel:%s\\n\' "$(uname -r)"; '
            'printf \'hostname:%s\\n\' "$(hostname)"'
        ),
        "timeout_s": 10,
        "metrics": [
            {
                "name": "os",
                "regex": r"(?m)^os:(.+)$",
                "group_index": 1,
                "cast": "str",
                "unit": "",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "kernel",
                "regex": r"(?m)^kernel:(.+)$",
                "group_index": 1,
                "cast": "str",
                "unit": "",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "hostname",
                "regex": r"(?m)^hostname:(.+)$",
                "group_index": 1,
                "cast": "str",
                "unit": "",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
        ],
    },
    {
        # One command (/proc/loadavg), three load-average metrics.
        "name": "cpu load",
        "script": "cat /proc/loadavg",
        "timeout_s": 10,
        "metrics": [
            {
                "name": "load1",
                "regex": r"([0-9.]+)\s+[0-9.]+\s+[0-9.]+",
                "group_index": 1,
                "cast": "float",
                "unit": "load",
                "rules": [
                    {"op": "gt", "value": "8", "level": "critical"},
                    {"op": "gt", "value": "4", "level": "warning"},
                    {"op": "present", "value": "", "level": "good"},
                ],
            },
            {
                "name": "load5",
                "regex": r"[0-9.]+\s+([0-9.]+)\s+[0-9.]+",
                "group_index": 1,
                "cast": "float",
                "unit": "load",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "load15",
                "regex": r"[0-9.]+\s+[0-9.]+\s+([0-9.]+)",
                "group_index": 1,
                "cast": "float",
                "unit": "load",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
        ],
    },
    {
        # One command (free), labelled memory metrics in bytes.
        "name": "memory usage",
        "script": (
            "free -b | awk '/^Mem:/{print \"total:\" $2; print \"used:\" $3; "
            "print \"free:\" $4; print \"available:\" $7}'"
        ),
        "timeout_s": 10,
        "metrics": [
            {
                "name": "total_bytes",
                "regex": r"(?m)^total:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "B",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "used_bytes",
                "regex": r"(?m)^used:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "B",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "free_bytes",
                "regex": r"(?m)^free:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "B",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "available_bytes",
                "regex": r"(?m)^available:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "B",
                "rules": [
                    {"op": "lt", "value": "134217728", "level": "critical"},
                    {"op": "lt", "value": "536870912", "level": "warning"},
                    {"op": "present", "value": "", "level": "good"},
                ],
            },
        ],
    },
    {
        # One command (df), labelled disk metrics for the root filesystem.
        "name": "disk usage",
        "script": (
            "df -P / | awk 'NR==2{gsub(/%/,\"\",$5); print \"total_kb:\" $2; "
            "print \"used_kb:\" $3; print \"avail_kb:\" $4; print \"used_pct:\" $5}'"
        ),
        "timeout_s": 10,
        "metrics": [
            {
                "name": "total_kb",
                "regex": r"(?m)^total_kb:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "KB",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "used_kb",
                "regex": r"(?m)^used_kb:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "KB",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "avail_kb",
                "regex": r"(?m)^avail_kb:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "KB",
                "rules": [{"op": "present", "value": "", "level": "good"}],
            },
            {
                "name": "used_pct",
                "regex": r"(?m)^used_pct:([0-9]+)",
                "group_index": 1,
                "cast": "int",
                "unit": "%",
                "rules": [
                    {"op": "gt", "value": "90", "level": "critical"},
                    {"op": "gt", "value": "80", "level": "warning"},
                    {"op": "present", "value": "", "level": "good"},
                ],
            },
        ],
    },
    {
        # Network reachability stays its own dataset (a distinct probe, not OS info).
        "name": "ping localhost",
        "script": "ping -c1 -W2 localhost",
        "timeout_s": 10,
        "metrics": [
            {
                "name": "rtt",
                "regex": r"time[=<]\s*([0-9.]+)",
                "group_index": 1,
                "cast": "float",
                "unit": "ms",
                "rules": [
                    {"op": "missing", "value": "", "level": "critical"},
                    {"op": "gt", "value": "500", "level": "critical"},
                    {"op": "gt", "value": "100", "level": "warning"},
                    {"op": "present", "value": "", "level": "good"},
                ],
            }
        ],
    },
]


def _seed_demo(conn: sqlite3.Connection) -> None:
    """Seed a demo ``localhost`` node with system datasets/metrics/rules.

    Only runs when ``SM_SEED_DEMO`` is set (so pytest fixtures, which create
    fresh databases, stay clean) and the database has no nodes yet — i.e. on
    first boot of a real instance. Idempotent: a second boot finds existing
    nodes and does nothing."""
    if not os.environ.get("SM_SEED_DEMO"):
        return
    if conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]:
        return
    levels = {
        r["name"]: r["id"]
        for r in conn.execute("SELECT id, name FROM status_levels").fetchall()
    }
    if not {"good", "warning", "critical"} <= set(levels):
        return

    node_id = "localhost"
    now = now_iso()
    conn.execute(
        "INSERT INTO nodes (id, label, network_resource, network_path, tracking, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (node_id, "localhost", "server", "localhost", 1, now, now),
    )

    # Add the node to the graph layout so the admin canvas renders it.
    model = get_graph_json(conn)
    if not any(n.get("id") == node_id for n in model.get("nodes", [])):
        model.setdefault("nodes", []).append(
            {"id": node_id, "label": "localhost", "x": 0, "y": 0, "params": []}
        )
        set_graph_json(conn, model)

    for spec in DEMO_DATASETS:
        cur = conn.execute(
            "INSERT INTO datasets (node_id, name, method, script, timeout_s, "
            "interval_s, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                spec["name"],
                "bash",
                spec["script"],
                spec.get("timeout_s", 30),
                spec.get("interval_s", 600),
                1,
                now,
                now,
            ),
        )
        ds_id = cur.lastrowid
        for m in spec["metrics"]:
            mcur = conn.execute(
                "INSERT INTO metrics (dataset_id, name, regex, group_index, cast, "
                "unit, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    ds_id,
                    m["name"],
                    m["regex"],
                    m.get("group_index", 1),
                    m.get("cast", "auto"),
                    m.get("unit", ""),
                    now,
                ),
            )
            metric_id = mcur.lastrowid
            for r in m.get("rules", []):
                conn.execute(
                    "INSERT INTO rules (metric_id, op, value, status_level_id, "
                    "priority) VALUES (?,?,?,?,?)",
                    (
                        metric_id,
                        r["op"],
                        r.get("value", ""),
                        levels[r["level"]],
                        r.get("priority", 0),
                    ),
                )
