"""Templating helpers.

Two responsibilities:

1. ``render`` — substitute ``{{a.b.c}}`` placeholders from a nested context
   dict. Used by mailer and (potentially) dataset scripts. Secrets are NEVER
   placed into the render context, so a template can never exfiltrate a stored
   credential.
2. Template CRUD — save/apply reusable dataset, metric, and rule definitions.
   A template payload is a JSON blob describing the object (and, for datasets,
   its metrics and rules). Applying a template to a node copies the structure
   with placeholders resolved from the target node.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}}")

# ``[Square Bracket]`` parameter tokens. The name must start with a letter and
# may only contain word-ish characters (spaces/hyphens allowed, e.g.
# ``[Network Path]``), so shell idioms like ``[ -f /x ]``, ``[[ ]]``,
# ``[1]`` and list literals like ``[id,name]`` are never mistaken for parameters.
_PARAM = re.compile(r"\[(?=[A-Za-z])([A-Za-z0-9 _.\-]+)\]")

# Auto-fillable parameter names (lower-cased). See ``known_params``. The raw
# DB field names are intentionally NOT parameters — only ``label``/``id`` and
# the friendly UI label (``network path``) are, so the terminology stays
# consistent with the node form.
KNOWN_PARAM_KEYS = frozenset(
    {"label", "id", "network path"}
)


def is_param_name(name: str) -> bool:
    """Whether a ``[token]`` is a parameter rather than literal text.

    Parameters are intentionally-named: a known node field (any case), Title
    Case (``[Host]``), or multi-word (``[Network Path]``). Lower-case
    single words like ``[a-z]`` or ``[id]`` stay literal — this keeps regex
    character classes and URL path segments out of the parameter fields.
    """
    return name.lower() in KNOWN_PARAM_KEYS or name[:1].isupper() or " " in name


def _lookup(ctx: dict, dotted: str) -> str:
    cur: Any = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return "" if cur is None else str(cur)


def render(template: str, ctx: dict) -> str:
    """Replace ``{{a.b}}`` tokens with values from *ctx* (empty string if absent)."""
    if not template:
        return template
    return _PLACEHOLDER.sub(lambda m: _lookup(ctx, m.group(1)), template)


def node_context(node: sqlite3.Row) -> dict:
    return {
        "node": {"label": node["label"], "network_path": node["network_path"], "id": node["id"]},
    }


# ---------------------------------------------------------------------------
# ``[Square Bracket]`` parameters
# ---------------------------------------------------------------------------
def known_params(node: sqlite3.Row) -> dict:
    """Auto-fillable parameter values for a node, keyed by lower-cased name.

    The friendly names used by the node editor are the parameters — e.g.
    ``[Network Path]`` fills from the node's network path, matching
    the label shown on the node form. ``label`` and ``id`` are also accepted.
    """
    return {
        "label": node["label"],
        "id": node["id"],
        "network path": node["network_path"],
    }


def extract_params(*texts: Any) -> list[str]:
    """Return the distinct intentional ``[Param]`` names found in *texts*, in
    first-seen order. Regex character classes and other lower-case single-word
    brackets (``[a-z]``, ``[0-9]``) are skipped — see ``is_param_name``."""
    seen: dict[str, None] = {}
    for text in texts:
        if not isinstance(text, str):
            continue
        for m in _PARAM.finditer(text):
            name = m.group(1).strip()
            if name and is_param_name(name):
                seen.setdefault(name, None)
    return list(seen)


def render_params(text: str, values: dict) -> str:
    """Replace ``[Name]`` tokens from *values* (case-insensitive lookup).

    Only intentional parameter names (see ``is_param_name``) are substituted;
    other bracketed text (regex classes, shell idioms) is left untouched.
    Unknown parameters are also left untouched — callers validate required
    parameters before rendering.
    """
    if not text:
        return text

    def _sub(m: re.Match) -> str:
        name = m.group(1).strip()
        if not is_param_name(name):
            return m.group(0)
        key = name.lower()
        if key in values:
            val = values[key]
            return "" if val is None else str(val)
        return m.group(0)

    return _PARAM.sub(_sub, text)


def strip_self_referential(values: dict) -> dict:
    """Drop entries whose value is the bracketed token naming themselves.

    A saved value like ``{"label": "[label]"}`` — usually created by typing a
    token into a parameter field and saving — would shadow the node-derived
    value with the literal text ``[label]``, so the resolved script still
    contains ``[label]`` (e.g. ``ping [label]`` failing on Windows). Such an
    entry can never substitute anything useful, so it is ignored and any real
    value for that key (typically the node's own field) wins."""

    def _self_ref(k: object, v: object) -> bool:
        if v is None:
            return False
        m = _PARAM.fullmatch(str(v).strip())
        return bool(m and m.group(1).strip().lower() == str(k).strip().lower())

    return {k: v for k, v in (values or {}).items() if not _self_ref(k, v)}


def _dataset_param_texts(payload: dict) -> list[str]:
    """All template strings in a dataset payload that may contain ``[Param]`` tokens."""
    texts: list[str] = [payload.get("name", ""), payload.get("script", "")]
    for m in payload.get("metrics", []):
        texts.extend([m.get("name", ""), m.get("regex", ""), m.get("unit", "")])
        for r in m.get("rules", []):
            texts.append(r.get("value", ""))
    return [t for t in texts if isinstance(t, str)]


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------
def list_templates(conn: sqlite3.Connection, template_type: str | None = None) -> list[dict]:
    if template_type:
        rows = conn.execute("SELECT * FROM templates WHERE template_type=? ORDER BY name", (template_type,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM templates ORDER BY template_type, name").fetchall()
    return [_tpl_dict(r) for r in rows]


def get_template(conn: sqlite3.Connection, template_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
    return _tpl_dict(row) if row else None


def save_template(conn: sqlite3.Connection, name: str, template_type: str, payload: dict) -> dict:
    conn.execute(
        """INSERT INTO templates (name, template_type, payload, created_at) VALUES (?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET template_type=excluded.template_type, payload=excluded.payload""",
        (name, template_type, json.dumps(payload), db.now_iso()),
    )
    row = conn.execute("SELECT * FROM templates WHERE name=?", (name,)).fetchone()
    return _tpl_dict(row)


def delete_template(conn: sqlite3.Connection, template_id: int) -> bool:
    cur = conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
    return cur.rowcount > 0


def _tpl_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "template_type": row["template_type"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


def apply_dataset_template(
    conn: sqlite3.Connection,
    template_id: int,
    node_id: str,
    params: dict | None = None,
) -> dict:
    """Apply a dataset template (with nested metrics/rules) to a node.

    ``{{...}}`` placeholders resolve against the target node, and
    ``[Square Bracket]`` parameters resolve from the node's known fields
    (label/id plus the friendly network-path/network-resource labels) plus the
    caller-supplied *params* mapping. Any
    ``[Param]`` token with no value raises ``ValueError`` listing the missing
    names, so the UI can prompt for them. Returns the created dataset summary.
    """
    tpl = get_template(conn, template_id)
    if not tpl or tpl["template_type"] != "dataset":
        raise ValueError("template not found or not a dataset template")
    node = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not node:
        raise ValueError("target node not found")
    ctx = node_context(node)
    payload = tpl["payload"]

    values = dict(known_params(node))
    for key, val in (params or {}).items():
        values[str(key).strip().lower()] = val

    required = extract_params(*_dataset_param_texts(payload))
    missing = [name for name in required if name.lower() not in values]
    if missing:
        raise ValueError("missing parameters: " + ", ".join(missing))

    def _r(text: Any) -> str:
        return render_params(render(text or "", ctx), values)

    ds_name = _r(payload.get("name", "Dataset"))
    cur = conn.execute(
        """INSERT INTO datasets (node_id, name, method, script, timeout_s, interval_s, enabled, next_run_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (node_id, ds_name, payload.get("method", "bash"), _r(payload.get("script", "")),
         payload.get("timeout_s", 30), payload.get("interval_s", 600), 1, db.now_iso(), db.now_iso(), db.now_iso()),
    )
    ds_id = cur.lastrowid
    for m in payload.get("metrics", []):
        mcur = conn.execute(
            "INSERT INTO metrics (dataset_id, name, regex, group_index, cast, unit, created_at) VALUES (?,?,?,?,?,?,?)",
            (ds_id, _r(m.get("name", "metric")), _r(m.get("regex", "")), m.get("group_index", 1),
             m.get("cast", "auto"), _r(m.get("unit", "")), db.now_iso()),
        )
        mid = mcur.lastrowid
        for r in m.get("rules", []):
            conn.execute(
                "INSERT INTO rules (metric_id, op, value, status_level_id, priority) VALUES (?,?,?,?,?)",
                (mid, r.get("op", "missing"), _r(r.get("value", "")), r.get("status_level_id"), r.get("priority", 0)),
            )
    return {"dataset_id": ds_id, "name": ds_name}


# ---------------------------------------------------------------------------
# Bundled default templates
# ---------------------------------------------------------------------------
# Dataset templates shipped with the app and seeded into every new database by
# ``db._seed_default_templates``. Rule levels are stored by *name* (resolved to
# ``status_levels`` ids at seed time, never hardcoded). Users can edit or delete
# them freely — seeding only inserts names that do not exist yet, so deletions
# stick across restarts and user edits survive.
DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": 'WinOS Disks',
        "payload": {'name': 'Disks', 'method': 'powershell', 'script': '$ComputerName = "[label]"\n\n$IsLocal = $ComputerName -in @("localhost", "127.0.0.1", ".", $env:COMPUTERNAME)\n\nif ($IsLocal) {\n    $Disks = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DriveType=3"\n} else {\n    try {\n        $Session = New-CimSession -ComputerName $ComputerName -ErrorAction Stop\n    } catch {\n        $DcomOpt = New-CimSessionOption -Protocol Dcom\n        $Session = New-CimSession -ComputerName $ComputerName -SessionOption $DcomOpt -ErrorAction Stop\n    }\n\n    $Disks = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DriveType=3" -CimSession $Session\n    Remove-CimSession -CimSession $Session\n}\n\n$Metrics = foreach ($Disk in $Disks) {\n    if ($Disk.Size -gt 0) {\n        $Drive   = $Disk.DeviceID.TrimEnd(\':\')\n        $TotalGB = [math]::Round($Disk.Size / 1GB, 2)\n        $UsedPct = [math]::Round((($Disk.Size - $Disk.FreeSpace) / $Disk.Size) * 100, 1)\n        \n        "${Drive}_Capacity_GB=$TotalGB"\n        "${Drive}_Used_Pct=$UsedPct"\n    }\n}\n\n$Metrics -join "|"', 'timeout_s': 120, 'interval_s': 600, 'metrics': [{'name': 'Disk (C) Total', 'regex': 'C_Capacity_GB=([\\d.]+)', 'group_index': 1, 'cast': 'float', 'unit': ' GB', 'rules': []}, {'name': 'Disk (C) Used', 'regex': 'C_Used_Pct=([\\d.]+)', 'group_index': 1, 'cast': 'float', 'unit': '%', 'rules': [{'op': 'lt', 'value': '80', 'priority': 0, 'level': 'good'}, {'op': 'between', 'value': '80,90', 'priority': 0, 'level': 'warning'}, {'op': 'gte', 'value': '90', 'priority': 0, 'level': 'critical'}]}, {'name': 'Disk (D) Total', 'regex': 'D_Capacity_GB=([\\d.]+)', 'group_index': 1, 'cast': 'float', 'unit': ' GB', 'rules': []}, {'name': 'Disk (D) Used', 'regex': 'D_Used_Pct=([\\d.]+)', 'group_index': 1, 'cast': 'float', 'unit': '%', 'rules': [{'op': 'lt', 'value': '80', 'priority': 0, 'level': 'good'}, {'op': 'between', 'value': '80,90', 'priority': 0, 'level': 'warning'}, {'op': 'gte', 'value': '90', 'priority': 0, 'level': 'critical'}]}]},
    },
    {
        "name": 'WinOS OS/CPU/RAM',
        "payload": {'name': 'OS/CPU/RAM', 'method': 'powershell', 'script': '$ComputerName = "[label]"\n\n$IsLocal = $ComputerName -in @("localhost", "127.0.0.1", ".", $env:COMPUTERNAME)\n\nif ($IsLocal) {\n    # Query local WMI directly without WinRM\n    $OS  = Get-CimInstance -ClassName Win32_OperatingSystem\n    $CPU = Get-CimInstance -ClassName Win32_Processor\n    $CS  = Get-CimInstance -ClassName Win32_ComputerSystem\n} else {\n    try {\n        $Session = New-CimSession -ComputerName $ComputerName -ErrorAction Stop\n    } catch {\n        $DcomOpt = New-CimSessionOption -Protocol Dcom\n        $Session = New-CimSession -ComputerName $ComputerName -SessionOption $DcomOpt -ErrorAction Stop\n    }\n\n    $OS  = Get-CimInstance -ClassName Win32_OperatingSystem -CimSession $Session\n    $CPU = Get-CimInstance -ClassName Win32_Processor -CimSession $Session\n    $CS  = Get-CimInstance -ClassName Win32_ComputerSystem -CimSession $Session\n    Remove-CimSession -CimSession $Session\n}\n\n$OSName   = $OS.Caption.Trim()\n$CpuCores = ($CPU | Measure-Object -Property NumberOfCores -Sum).Sum\n$RamGB    = [math]::Round($CS.TotalPhysicalMemory / 1GB, 2)\n\n"OS=$OSName|Cores=$CpuCores|RAM_GB=$RamGB"', 'timeout_s': 120, 'interval_s': 604800, 'metrics': [{'name': 'OS', 'regex': 'OS=([^|\\r\\n]+)', 'group_index': 1, 'cast': 'str', 'unit': '', 'rules': []}, {'name': 'CPU Cores', 'regex': 'Cores=(\\d+)', 'group_index': 1, 'cast': 'int', 'unit': '', 'rules': []}, {'name': 'Memory', 'regex': 'RAM_GB=([\\d.]+)', 'group_index': 1, 'cast': 'float', 'unit': ' GB', 'rules': []}]},
    },
    {
        "name": 'WinOS Ping',
        "payload": {'name': 'Ping', 'method': 'cmd', 'script': 'ping -4 -n 1 [label] | findstr TTL >nul && echo True || echo False', 'timeout_s': 30, 'interval_s': 600, 'metrics': [{'name': 'Ping', 'regex': '(.*)', 'group_index': 1, 'cast': 'bool', 'unit': '', 'rules': [{'op': 'is', 'value': 'True', 'priority': 0, 'level': 'good'}, {'op': 'is', 'value': 'False', 'priority': 0, 'level': 'critical'}]}]},
    },
]
