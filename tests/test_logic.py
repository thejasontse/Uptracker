"""Unit tests for the pure logic: parser, rules, templates, crypto."""
import pytest

from app import templates
from app.crypto import secret_box
from app.parser import extract
from app.rules import RuleRow, evaluate_metric, rule_matches, worst_status


# ---------------- parser ----------------
def test_extract_regex_capture():
    v = extract("ping ok rtt=42ms", r"rtt=(\d+)", 1, "auto")
    assert v.ok and v.num == 42 and v.txt == "42"


def test_extract_no_match():
    v = extract("nothing here", r"rtt=(\d+)")
    assert not v.ok and v.error == "no match"


def test_extract_invalid_regex():
    v = extract("x", r"([")
    assert not v.ok and "invalid regex" in v.error


def test_extract_group_out_of_range():
    v = extract("a=1", r"a=(\d+)", 5)
    assert not v.ok


def test_cast_int_float_bool_str():
    assert extract("v=3", r"v=(\d+)", 1, "int").num == 3
    assert extract("v=3.5", r"v=([\d.]+)", 1, "float").num == 3.5
    assert extract("v=true", r"v=(\w+)", 1, "bool").num == 1.0
    assert extract("v=3", r"v=(\d+)", 1, "str").num is None


# ---------------- rules ----------------
def _rule(op, value, sid=1, prio=0):
    return RuleRow(1, op, value, sid, prio)


def test_rule_numeric_ops():
    v = extract("x 50", r"(\d+)", 1, "auto")
    assert rule_matches(_rule("gt", "10"), v)
    assert not rule_matches(_rule("lt", "10"), v)
    assert rule_matches(_rule("gte", "50"), v)
    assert rule_matches(_rule("eq", "50"), v)
    assert rule_matches(_rule("ne", "51"), v)


def test_rule_string_ops():
    v = extract("status=UP", r"status=(\w+)", 1, "str")
    assert rule_matches(_rule("contains", "U"), v)
    assert rule_matches(_rule("regex", "^U"), v)
    assert rule_matches(_rule("eq", "UP"), v)


def test_rule_missing_present():
    v = extract("nope", r"rtt=(\d+)")
    assert rule_matches(_rule("missing", ""), v)
    assert not rule_matches(_rule("present", ""), v)
    assert not rule_matches(_rule("gt", "1"), v)  # value-comparison can't match missing


def test_evaluate_metric_worst_status_wins():
    # Two rules match; the worse (higher-rank) status wins regardless of priority.
    ranks = {1: 10, 2: 20, 3: 30}
    v = extract("x 50", r"(\d+)", 1, "auto")
    rules = [_rule("gt", "10", sid=2, prio=5), _rule("gt", "40", sid=3, prio=0)]
    assert evaluate_metric(rules, v, ranks) == 3
    # Priority order flipped — still the worst status wins.
    rules = [_rule("gt", "10", sid=2, prio=0), _rule("gt", "40", sid=3, prio=5)]
    assert evaluate_metric(rules, v, ranks) == 3


def test_evaluate_metric_no_match_returns_none():
    ranks = {1: 10, 2: 20, 3: 30}
    v = extract("x 1", r"(\d+)", 1, "auto")
    assert evaluate_metric([_rule("gt", "10", sid=2)], v, ranks) is None


def test_worst_status():
    ranks = {1: 10, 2: 20, 3: 30}
    assert worst_status([1, 3, 2], ranks) == 3
    assert worst_status([], ranks) is None


# ---------------- crypto ----------------
def test_secret_roundtrip(data_dir):
    box = secret_box()
    enc = box.encrypt("hunter2")
    assert enc != "hunter2"
    assert box.decrypt(enc) == "hunter2"


def test_keyfile_permissions(data_dir):
    secret_box()  # creates key
    mode = (data_dir / "secret.key").stat().st_mode & 0o777
    assert mode == 0o600


# ---------------- templates ----------------
def test_render_placeholders():
    ctx = {"node": {"label": "web01", "network_path": "10.0.0.1"}, "status": "down"}
    assert templates.render("{{node.label}} at {{node.network_path}} is {{status}}", ctx) == "web01 at 10.0.0.1 is down"
    assert templates.render("{{missing.thing}}", ctx) == ""


def test_template_roundtrip_and_apply(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_resource,network_path,created_at,updated_at) VALUES ('n1','web01','server','10.0.0.1','x','x')")
        payload = {"name": "Ping {{node.label}}", "method": "bash", "script": "{{node.network_path}}",
                   "interval_s": 600, "timeout_s": 10,
                   "metrics": [{"name": "rtt", "regex": r"time=(\d+)", "group_index": 1, "cast": "float", "unit": "ms",
                                "rules": [{"op": "gt", "value": "100", "status_level_id": 3, "priority": 0}]}]}
        tpl = templates.save_template(conn, "ping-base", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1")
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Ping web01" and ds["script"] == "10.0.0.1"
        m = conn.execute("SELECT * FROM metrics WHERE dataset_id=?", (ds["id"],)).fetchone()
        assert m["name"] == "rtt"
        r = conn.execute("SELECT * FROM rules WHERE metric_id=?", (m["id"],)).fetchone()
        assert r["op"] == "gt" and r["status_level_id"] == 3


# ---------------- [square bracket] parameters ----------------
def test_extract_params_dedupes_and_keeps_order():
    names = templates.extract_params("ping [Network-Path Reference]", "echo [Host] [Network-Path Reference] [label]")
    assert names == ["Network-Path Reference", "Host", "label"]


def test_extract_params_ignores_shell_idioms():
    # Shell test brackets, double brackets, and numeric indices are NOT parameters.
    assert templates.extract_params("if [ -f /etc/passwd ]; then echo ok; fi") == []
    assert templates.extract_params("[[ 1 -eq 1 ]] && echo yes") == []
    assert templates.extract_params("awk '{print $1}' file[1]") == []
    assert templates.extract_params("curl -s 'https://x/api?fields=[id,name]'") == []


def test_extract_params_ignores_regex_char_classes():
    # Lower-case single-word brackets (regex classes, URL path segments) stay literal.
    assert templates.extract_params(r"time=([0-9.]+)ms") == []
    assert templates.extract_params(r"status=[a-z]+") == []
    # But Title Case / multi-word / known fields are parameters.
    assert templates.extract_params("ping [Host] [Network-Path Reference] [label]") == ["Host", "Network-Path Reference", "label"]


def test_render_params_is_case_insensitive_and_leaves_unknown():
    values = {"host": "10.0.0.5"}
    assert templates.render_params("ping [Host]", values) == "ping 10.0.0.5"
    assert templates.render_params("ping [Missing]", values) == "ping [Missing]"
    # Regex char classes are never substituted, even if a value exists.
    assert templates.render_params(r"time=([0-9.]+)", {"a-z": "X"}) == r"time=([0-9.]+)"


def test_apply_template_friendly_label_aliases(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_resource,network_path,created_at,updated_at) VALUES ('n1','web01','server','10.0.0.1','x','x')")
        # [Network-Path Reference] is the node form's label for the network-path field.
        payload = {"name": "Ping [Network-Path Reference]", "method": "bash",
                   "script": "ping -c1 [Network-Path Reference] as [Network Resource]"}
        tpl = templates.save_template(conn, "friendly", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1")  # no params needed
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Ping 10.0.0.1"
        assert ds["script"] == "ping -c1 10.0.0.1 as server"


def test_apply_template_with_params(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_resource,network_path,created_at,updated_at) VALUES ('n1','web01','server','10.0.0.1','x','x')")
        payload = {"name": "Ping [Network-Path Reference]", "method": "bash",
                   "script": "ping -c1 [Network-Path Reference]",
                   "metrics": [{"name": "rtt [Network-Path Reference]", "regex": r"time=(\d+)", "group_index": 1,
                                "cast": "float", "unit": "ms",
                                "rules": [{"op": "gt", "value": "[Threshold]", "status_level_id": 3, "priority": 0}]}]}
        tpl = templates.save_template(conn, "path-ping", "dataset", payload)

        # Missing params are reported by name. [Network-Path Reference] auto-fills
        # from the node's network path (friendly alias), so only [Threshold] is missing here.
        try:
            templates.apply_dataset_template(conn, tpl["id"], "n1")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "Threshold" in str(exc) and "Network-Path Reference" not in str(exc)

        res = templates.apply_dataset_template(
            conn, tpl["id"], "n1", {"Network-Path Reference": "db.internal", "Threshold": "50"})
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Ping db.internal" and ds["script"] == "ping -c1 db.internal"
        m = conn.execute("SELECT * FROM metrics WHERE dataset_id=?", (ds["id"],)).fetchone()
        assert m["name"] == "rtt db.internal"
        r = conn.execute("SELECT * FROM rules WHERE metric_id=?", (m["id"],)).fetchone()
        assert r["value"] == "50"


def test_apply_template_node_fields_auto_fill(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_resource,network_path,created_at,updated_at) VALUES ('n1','web01','server','10.0.0.1','x','x')")
        payload = {"name": "Check [label]", "method": "bash", "script": "check [Network-Path Reference] as [Network Resource] ([id])"}
        tpl = templates.save_template(conn, "node-fields", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1")  # no params needed
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Check web01"
        assert ds["script"] == "check 10.0.0.1 as server (n1)"


def test_apply_template_mixes_mustaches_and_brackets(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_resource,network_path,created_at,updated_at) VALUES ('n1','web01','server','10.0.0.1','x','x')")
        payload = {"name": "{{node.label}} probe", "method": "bash",
                   "script": "curl -s http://{{node.network_path}}/[Port]/health"}
        tpl = templates.save_template(conn, "mixed", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1", {"Port": "8080"})
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["script"] == "curl -s http://10.0.0.1/8080/health"
