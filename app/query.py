"""Read-only SQL query layer for the bottom nodes table.

The admin query bar and the dashboard "Views" run user-authored SQL against a
per-request TEMP table ``nodes_wide`` — one row per node with the columns shown
in the table (id, label, network_resource, network_path, status,
datasets) PLUS one column per distinct metric name (case-insensitive; a node
that lacks a metric shows ``'N/A'``).

Safety model (defence in depth):
  * The query runs on its own connection. TEMP objects live in the connection's
    private temp database and never touch the main DB.
  * ``PRAGMA query_only=ON`` blocks any write to the main database.
  * A ``set_authorizer`` allowlist permits only SELECT/READ/FUNCTION, so DDL,
    DML, ATTACH, and dangerous functions (load_extension, …) are refused.
  * Only a single statement is accepted (sqlite3 rejects multiples).
  * The user SQL is wrapped in ``SELECT * FROM (…) LIMIT n``.

The query FILTERS ROWS ONLY: its first column is taken as the set of node ids
to show in the table (any column name/alias is accepted).
"""
from __future__ import annotations

import re
import sqlite3

from . import db

# Hard cap on rows a query may return (the wrapper adds LIMIT).
MAX_ROWS = 5000

# Built-in functions that must never be callable from user SQL.
_DENIED_FUNCTIONS = {
    "load_extension", "fts3_tokenizer", "readfile", "writefile", "edit",
    "setofint", "eval",
}

# Authorizer action codes that are always allowed for a read-only query.
# SQLITE_RECURSIVE exists on newer SQLite only; resolved defensively below.
_ALLOW_CODES = {
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", -1),
}


class QueryError(ValueError):
    """Raised for any user-query problem (syntax, denied op, bad shape)."""


# The single table a user query runs against. Users never need to name it: when
# their SQL omits a FROM clause we inject ``FROM nodes_wide`` so they can write
# just ``SELECT id WHERE status = 'critical'`` and see the rows on screen.
_WIDE_TABLE = "nodes_wide"

# Clause keywords that may follow the (implicit) FROM, in priority order. Used to
# find where to splice ``FROM nodes_wide`` into a FROM-less query.
_TRAILING_CLAUSE = re.compile(
    r"\b(where|group\s+by|order\s+by|having|limit)\b", re.IGNORECASE)


def _ensure_from(sql: str) -> str:
    """Inject ``FROM nodes_wide`` when the query has no top-level FROM clause.

    Lets users write ``SELECT id WHERE status = 'critical'`` without knowing the
    backing table's name. If a FROM is already present (explicit or in a
    subquery), the SQL is returned untouched so advanced queries keep working.
    """
    if re.search(r"\bfrom\b", sql, re.IGNORECASE):
        return sql
    m = _TRAILING_CLAUSE.search(sql)
    if not m:
        return f"{sql} FROM {_WIDE_TABLE}"
    at = m.start()
    return f"{sql[:at].rstrip()} FROM {_WIDE_TABLE} {sql[at:]}"



def _q(name: str) -> str:
    """Quote an identifier for interpolation into server-built SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def _authorizer(code: int, arg1: str | None, arg2: str | None,
                db_name: str | None, source: str | None) -> int:
    if code in _ALLOW_CODES:
        # Function calls are allowed except a small denylist of dangerous ones.
        if code == sqlite3.SQLITE_FUNCTION and (arg1 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _metric_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Distinct metric names, deduped case-insensitively.

    Returns ``[(lower_name, display_name), …]``. The display name is the
    first-seen casing (ORDER BY name); SQLite resolves column references
    case-insensitively, so ``"RTT"`` and ``"rtt"`` map to one column.
    """
    canon: dict[str, str] = {}
    for r in conn.execute("SELECT DISTINCT name FROM metrics ORDER BY name").fetchall():
        canon.setdefault(r["name"].lower(), r["name"])
    return list(canon.items())


def _build_wide(conn: sqlite3.Connection) -> list[str]:
    """Create TEMP TABLE nodes_wide (node columns + one per metric). Returns
    the ordered list of column names exposed to the user query."""
    metrics = _metric_columns(conn)
    cols = ["id", "node_id", "label", "network_resource", "network_path",
            "status", "datasets"] + [disp for _, disp in metrics]

    coldefs = [
        "id TEXT PRIMARY KEY",
        "node_id TEXT",
        "label TEXT",
        "network_resource TEXT",
        "network_path TEXT",
        "status TEXT",
        "datasets INTEGER",
    ] + [f"{_q(disp)} TEXT DEFAULT 'N/A'" for _, disp in metrics]

    conn.execute("DROP TABLE IF EXISTS temp.nodes_wide")
    conn.execute(f"CREATE TEMP TABLE nodes_wide ({', '.join(coldefs)})")
    conn.execute(
        """
        INSERT INTO nodes_wide
            (id, node_id, label, network_resource, network_path, status, datasets)
        SELECT n.id, n.id, n.label, n.network_resource, n.network_path,
               COALESCE(sl.name, 'unknown'),
               (SELECT COUNT(*) FROM datasets d WHERE d.node_id = n.id)
        FROM nodes n
        LEFT JOIN status_levels sl ON sl.id = n.status_level_id
        """
    )
    if not metrics:
        return cols

    # Latest collected value per (node, metric-name) in a SINGLE scan of
    # metric_values, materialised into a small indexed temp table. The previous
    # implementation ran one UPDATE per metric, each re-scanning metric_values
    # joined to metrics/datasets with ``LOWER(m.name)=?`` — O(metrics x values),
    # which is the dominant cost of opening the query bar on large graphs. Here
    # we pay for one pass and then fill each column with a cheap indexed lookup.
    conn.execute("DROP TABLE IF EXISTS temp._latest")
    conn.execute(
        """
        CREATE TEMP TABLE _latest AS
        SELECT node_id, lname, value
        FROM (
            SELECT d.node_id AS node_id,
                   LOWER(m.name) AS lname,
                   CASE WHEN mv.num IS NOT NULL THEN CAST(mv.num AS TEXT)
                        ELSE COALESCE(mv.txt, 'N/A') END AS value,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.node_id, LOWER(m.name)
                       ORDER BY mv.collected_at DESC, mv.metric_id DESC
                   ) AS rn
            FROM metric_values mv
            JOIN metrics m ON m.id = mv.metric_id
            JOIN datasets d ON d.id = mv.dataset_id
        )
        WHERE rn = 1
        """
    )
    conn.execute("CREATE INDEX temp._latest_idx ON _latest(node_id, lname)")
    for lower, disp in metrics:
        conn.execute(
            f"""
            UPDATE nodes_wide SET {_q(disp)} = (
                SELECT value FROM _latest
                WHERE _latest.node_id = nodes_wide.id AND _latest.lname = ?
            )
            WHERE EXISTS (
                SELECT 1 FROM _latest
                WHERE _latest.node_id = nodes_wide.id AND _latest.lname = ?
            )
            """,
            (lower, lower),
        )
    conn.execute("DROP TABLE IF EXISTS temp._latest")
    return cols


def list_columns() -> dict:
    """Column names available to a query (node fields + every metric name)."""
    conn = db.connect()
    try:
        cols = _build_wide(conn)
        return {"columns": cols}
    finally:
        conn.close()


def run_query(sql: str) -> dict:
    """Run a read-only SELECT and return ``{ids, count, columns}``.

    The first result column identifies the rows to show. It may be a node id or
    a node label (the table's visible first column is Label), so each value is
    resolved back to a node id by matching ``id`` OR ``label``. Raises QueryError
    on empty input, multiple statements, syntax errors, denied operations, or a
    result with no columns.
    """
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise QueryError("query is empty")

    conn = db.connect()
    try:
        # Build the temp view BEFORE locking the connection down.
        _build_wide(conn)
        # Users only ever see one table, so they shouldn't have to name it: a
        # query without a FROM clause runs against nodes_wide.
        sql = _ensure_from(sql)
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_authorizer)

        wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {MAX_ROWS}"
        try:
            cur = conn.execute(wrapped)
        except sqlite3.Error as exc:
            raise QueryError(str(exc)) from exc

        desc = cur.description or []
        if not desc:
            raise QueryError("query must return at least one column (the node id)")
        columns = [d[0] for d in desc]
        data = cur.fetchall()
        ids = _resolve_node_ids(conn, [r[0] for r in data])
        rows = [dict(zip(columns, r)) for r in data]
        return {"ids": ids, "count": len(ids), "columns": columns, "rows": rows}
    finally:
        conn.close()


def _resolve_node_ids(conn: sqlite3.Connection, values: list) -> list[str]:
    """Map a query's first-column values to node ids.

    Each value may already be a node id or a node label (the visible first
    column of the table is Label); either resolves to the matching node id.
    Values that match nothing are dropped. Order is preserved and duplicates
    removed.
    """
    keys = [str(v) for v in values if v is not None]
    if not keys:
        return []
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT id, label FROM nodes_wide WHERE id IN ({placeholders}) "
        f"OR label IN ({placeholders})",
        (*keys, *keys),
    ).fetchall()
    by_key: dict[str, str] = {}
    for r in rows:
        by_key.setdefault(str(r["id"]), r["id"])
        by_key.setdefault(str(r["label"]), r["id"])
    seen: set[str] = set()
    ids: list[str] = []
    for k in keys:
        nid = by_key.get(k)
        if nid is not None and nid not in seen:
            seen.add(nid)
            ids.append(nid)
    return ids

