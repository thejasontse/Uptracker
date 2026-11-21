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


def test_rule_eq_ne_numeric_with_unit_text():
    # Percentage-style metrics: the captured text carries a unit or the metric
    # is cast to str, so eq/ne must compare numeric prefixes (disk usage %).
    pct = extract("85%", r"(\d+%)", 1, "auto")   # num None ("85%" won't float())
    assert rule_matches(_rule("eq", "85"), pct)
    assert rule_matches(_rule("eq", "85%"), pct)  # rule value with a unit too
    assert not rule_matches(_rule("eq", "80"), pct)
    assert rule_matches(_rule("ne", "80"), pct)
    assert not rule_matches(_rule("ne", "85"), pct)
    strv = extract("85", r"(\d+)", 1, "str")      # cast=str: num is None
    assert rule_matches(_rule("eq", "85"), strv)
    assert not rule_matches(_rule("eq", "86"), strv)
    # Genuine string equality still wins when neither side is numeric.
    up = extract("UP", r"(\w+)", 1, "str")
    assert rule_matches(_rule("eq", "UP"), up)
    assert not rule_matches(_rule("eq", "85"), up)


def test_rule_missing_present():
    v = extract("nope", r"rtt=(\d+)")
    assert rule_matches(_rule("missing", ""), v)
    assert not rule_matches(_rule("present", ""), v)
    assert not rule_matches(_rule("gt", "1"), v)  # value-comparison can't match missing


def test_rule_is_is_not_bool_words():
    up = extract("state=UP", r"state=(\w+)", 1, "str")
    down = extract("state=DOWN", r"state=(\w+)", 1, "str")
    # "up"/"down" are recognised boolean words; is/is_not compare tri-state bools.
    assert rule_matches(_rule("is", "true"), up)
    assert rule_matches(_rule("is_not", "false"), up)
    assert rule_matches(_rule("is", "false"), down)
    assert rule_matches(_rule("is_not", "true"), down)
    # An unrecognisable value never matches is/is_not.
    maybe = extract("state=maybe", r"state=(\w+)", 1, "str")
    assert not rule_matches(_rule("is", "true"), maybe)
    assert not rule_matches(_rule("is_not", "true"), maybe)


def test_rule_is_numeric_nonzero():
    # A numeric value with no bool word falls back to num != 0.
    v = extract("x 7", r"(\d+)", 1, "auto")
    assert rule_matches(_rule("is", "true"), v)
    assert not rule_matches(_rule("is", "false"), v)


def test_rule_between_range():
    v = extract("temp 50", r"(\d+)", 1, "auto")
    assert rule_matches(_rule("between", "40,60"), v)
    assert rule_matches(_rule("between", "50,50"), v)   # inclusive on both ends
    assert not rule_matches(_rule("between", "60,70"), v)
    # A malformed range (not exactly two numbers) never matches.
    assert not rule_matches(_rule("between", "40"), v)
    assert not rule_matches(_rule("between", "abc,def"), v)


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
            "INSERT INTO nodes (id,label,network_path,created_at,updated_at) VALUES ('n1','web01','10.0.0.1','x','x')")
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
    names = templates.extract_params("ping [Network Path]", "echo [Host] [Network Path] [label]")
    assert names == ["Network Path", "Host", "label"]


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
    assert templates.extract_params("ping [Host] [Network Path] [label]") == ["Host", "Network Path", "label"]


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
            "INSERT INTO nodes (id,label,network_path,created_at,updated_at) VALUES ('n1','web01','10.0.0.1','x','x')")
        # [Network Path] is the node form's label for the network-path field.
        payload = {"name": "Ping [Network Path]", "method": "bash",
                   "script": "ping -c1 [Network Path]"}
        tpl = templates.save_template(conn, "friendly", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1")  # no params needed
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Ping 10.0.0.1"
        assert ds["script"] == "ping -c1 10.0.0.1"


def test_apply_template_with_params(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_path,created_at,updated_at) VALUES ('n1','web01','10.0.0.1','x','x')")
        payload = {"name": "Ping [Network Path]", "method": "bash",
                   "script": "ping -c1 [Network Path]",
                   "metrics": [{"name": "rtt [Network Path]", "regex": r"time=(\d+)", "group_index": 1,
                                "cast": "float", "unit": "ms",
                                "rules": [{"op": "gt", "value": "[Threshold]", "status_level_id": 3, "priority": 0}]}]}
        tpl = templates.save_template(conn, "path-ping", "dataset", payload)

        # Missing params are reported by name. [Network Path] auto-fills
        # from the node's network path (friendly alias), so only [Threshold] is missing here.
        try:
            templates.apply_dataset_template(conn, tpl["id"], "n1")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "Threshold" in str(exc) and "Network Path" not in str(exc)

        res = templates.apply_dataset_template(
            conn, tpl["id"], "n1", {"Network Path": "db.internal", "Threshold": "50"})
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
            "INSERT INTO nodes (id,label,network_path,created_at,updated_at) VALUES ('n1','web01','10.0.0.1','x','x')")
        payload = {"name": "Check [label]", "method": "bash", "script": "check [Network Path] ([id])"}
        tpl = templates.save_template(conn, "node-fields", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1")  # no params needed
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["name"] == "Check web01"
        assert ds["script"] == "check 10.0.0.1 (n1)"


def test_apply_template_mixes_mustaches_and_brackets(data_dir):
    from app import db
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (id,label,network_path,created_at,updated_at) VALUES ('n1','web01','10.0.0.1','x','x')")
        payload = {"name": "{{node.label}} probe", "method": "bash",
                   "script": "curl -s http://{{node.network_path}}/[Port]/health"}
        tpl = templates.save_template(conn, "mixed", "dataset", payload)
        res = templates.apply_dataset_template(conn, tpl["id"], "n1", {"Port": "8080"})
        ds = conn.execute("SELECT * FROM datasets WHERE id=?", (res["dataset_id"],)).fetchone()
        assert ds["script"] == "curl -s http://10.0.0.1/8080/health"
