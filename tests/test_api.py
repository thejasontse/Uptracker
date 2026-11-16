"""API integration tests: CRUD, credential guard, collection, snapshot."""
import json


def _mk_node(client, node_id="n1", label="web01"):
    model = {"version": 1, "nodes": [{"id": node_id, "label": label, "x": 0, "y": 0, "params": []}],
             "edges": [], "groups": []}
    r = client.put("/api/graph", json={"model": model})
    assert r.status_code == 200
    return node_id


def _mk_nodes(client, pairs):
    """PUT the whole graph model at once (PUT replaces, so all nodes go together)."""
    model = {"version": 1,
             "nodes": [{"id": nid, "label": lbl, "x": 0, "y": 0, "params": []}
                       for nid, lbl in pairs],
             "edges": [], "groups": []}
    r = client.put("/api/graph", json={"model": model})
    assert r.status_code == 200


def test_snapshot_shape(client):
    s = client.get("/api/snapshot").json()
    assert "graph" in s and "nodes" in s and "status_levels" in s


def test_graph_sync_creates_and_prunes_nodes(client):
    _mk_node(client)
    nodes = client.get("/api/snapshot").json()["nodes"]
    assert [n["id"] for n in nodes] == ["n1"]
    # Remove from model -> pruned.
    client.put("/api/graph", json={"model": {"version": 1, "nodes": [], "edges": [], "groups": []}})
    assert client.get("/api/snapshot").json()["nodes"] == []


def test_dataset_crud_and_collection(client):
    _mk_node(client)
    ds = client.post("/api/nodes/n1/datasets", json={
        "name": "echo", "method": "bash", "script": "echo rtt=42ms",
        "interval_s": 600, "timeout_s": 10}).json()
    m = client.post(f"/api/datasets/{ds['id']}/metrics", json={
        "name": "rtt", "regex": r"rtt=(\d+)", "group_index": 1, "cast": "float", "unit": "ms"}).json()
    levels = client.get("/api/status-levels").json()["status_levels"]
    critical = next(l for l in levels if l["name"] == "critical")
    client.post(f"/api/metrics/{m['id']}/rules", json={"op": "gt", "value": "1000", "status_level_id": critical["id"]})

    res = client.post(f"/api/datasets/{ds['id']}/run").json()
    assert res["ok"] and res["metrics"][0]["num"] == 42
    assert res["metrics"][0]["status"] == "unknown"  # 42 not > 1000, no rule matches

    snap = client.get("/api/snapshot").json()
    node = snap["nodes"][0]
    assert node["datasets"][0]["metrics"][0]["value"]["txt"] == "42"
    assert node["status"]["name"] == "unknown"


def test_rule_triggers_critical_status(client):
    _mk_node(client)
    ds = client.post("/api/nodes/n1/datasets", json={
        "name": "fail", "method": "bash", "script": "echo rtt=9999"}).json()
    m = client.post(f"/api/datasets/{ds['id']}/metrics", json={"name": "rtt", "regex": r"rtt=(\d+)"}).json()
    critical = next(l for l in client.get("/api/status-levels").json()["status_levels"] if l["name"] == "critical")
    client.post(f"/api/metrics/{m['id']}/rules", json={"op": "gt", "value": "100", "status_level_id": critical["id"]})
    res = client.post(f"/api/datasets/{ds['id']}/run").json()
    assert res["metrics"][0]["status"] == "critical"
    assert client.get("/api/snapshot").json()["nodes"][0]["status"]["name"] == "critical"


def test_missing_metric_rule(client):
    _mk_node(client)
    ds = client.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo nothing"}).json()
    m = client.post(f"/api/datasets/{ds['id']}/metrics", json={"name": "rtt", "regex": r"rtt=(\d+)"}).json()
    critical = next(l for l in client.get("/api/status-levels").json()["status_levels"] if l["name"] == "critical")
    client.post(f"/api/metrics/{m['id']}/rules", json={"op": "missing", "status_level_id": critical["id"]})
    res = client.post(f"/api/datasets/{ds['id']}/run").json()
    assert res["metrics"][0]["status"] == "critical"


# ---------------- credential guard ----------------
def test_credential_requires_secret_on_create(client):
    _mk_node(client)
    cred = client.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "s3cret"}).json()
    r = client.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo hi", "credential_id": cred["id"]})
    assert r.status_code == 428
    r2 = client.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo hi",
                                                     "credential_id": cred["id"], "secret": "s3cret"})
    assert r2.status_code == 200


def test_credential_guard_on_script_change(client):
    _mk_node(client)
    cred = client.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "s3cret"}).json()
    ds = client.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo hi",
                                                     "credential_id": cred["id"], "secret": "s3cret"}).json()
    # Changing script without secret -> 428.
    r = client.patch(f"/api/datasets/{ds['id']}", json={"name": "d", "method": "bash", "script": "echo evil",
                                                        "credential_id": cred["id"]})
    assert r.status_code == 428
    # With re-entered secret -> allowed.
    r2 = client.patch(f"/api/datasets/{ds['id']}", json={"name": "d", "method": "bash", "script": "echo ok",
                                                         "credential_id": cred["id"], "secret": "s3cret"})
    assert r2.status_code == 200
    # Non-script change (rename) without secret -> allowed.
    r3 = client.patch(f"/api/datasets/{ds['id']}", json={"name": "renamed", "method": "bash", "script": "echo ok",
                                                         "credential_id": cred["id"]})
    assert r3.status_code == 200


def test_secret_never_returned(client):
    client.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "topsecret"})
    body = json.dumps(client.get("/api/credentials").json())
    assert "topsecret" not in body


def test_credentials_injected_into_script(client):
    _mk_node(client)
    cred = client.post("/api/credentials", json={"name": "svc", "username": "myuser", "secret": "mypass"}).json()
    ds = client.post("/api/nodes/n1/datasets", json={
        "name": "use-cred", "method": "bash", "script": 'echo "user=$SM_USER secret=$SM_SECRET"',
        "credential_id": cred["id"], "secret": "mypass"}).json()
    res = client.post(f"/api/datasets/{ds['id']}/run").json()
    assert "user=myuser" in res["raw"] and "secret=mypass" in res["raw"]


# ---------------- test-run (dry) ----------------
def test_test_run_dry(client):
    res = client.post("/api/test/run", json={"method": "bash", "script": "echo v=7",
                                             "metrics": [{"name": "v", "regex": r"v=(\d+)"}]}).json()
    assert res["ok"] and res["parsed"][0]["num"] == 7
    # Nothing persisted.
    assert client.get("/api/snapshot").json()["nodes"] == []


# ---------------- templates API ----------------
def test_template_save_and_apply(client):
    _mk_node(client)
    t = client.post("/api/templates", json={"name": "t1", "template_type": "dataset", "payload": {
        "name": "Check {{node.label}}", "method": "bash", "script": "echo ok",
        "metrics": [{"name": "m", "regex": "ok", "group_index": 0}]}}).json()
    r = client.post(f"/api/templates/{t['id']}/apply", json={"node_id": "n1"}).json()
    assert r["name"] == "Check web01"
    snap = client.get("/api/snapshot").json()
    assert any(d["name"] == "Check web01" for d in snap["nodes"][0]["datasets"])


def test_template_apply_with_params(client):
    _mk_node(client)
    t = client.post("/api/templates", json={"name": "path-ping", "template_type": "dataset", "payload": {
        "name": "Ping [Network-Path Reference]", "method": "bash",
        "script": "ping -c1 [Network-Path Reference]",
        "metrics": [{"name": "rtt", "regex": r"time=(\d+)", "group_index": 1,
                     "rules": [{"op": "gt", "value": "[Threshold]", "status_level_id": 3}]}]}}).json()
    r = client.post(f"/api/templates/{t['id']}/apply",
                    json={"node_id": "n1", "params": {"Network-Path Reference": "db.internal", "Threshold": "50"}})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Ping db.internal"
    ds = next(d for d in client.get("/api/snapshot").json()["nodes"][0]["datasets"]
              if d["id"] == r.json()["dataset_id"])
    assert ds["script"] == "ping -c1 db.internal"
    assert ds["metrics"][0]["rules"][0]["value"] == "50"


def test_template_apply_missing_params(client):
    _mk_node(client)
    t = client.post("/api/templates", json={"name": "needs-params", "template_type": "dataset", "payload": {
        "name": "Probe [Host]", "method": "bash", "script": "check [Host] [Port]"}}).json()
    r = client.post(f"/api/templates/{t['id']}/apply", json={"node_id": "n1"})
    assert r.status_code == 400
    assert "Host" in r.json()["detail"] and "Port" in r.json()["detail"]
    # No dataset was created.
    assert client.get("/api/snapshot").json()["nodes"][0]["datasets"] == []


# ---------------- node patch ----------------
def test_node_patch_and_tracking(client):
    _mk_node(client)
    client.patch("/api/nodes/n1", json={"network_resource": "dns", "network_path": "ns1.example.com", "tracking": True})
    n = client.get("/api/snapshot").json()["nodes"][0]
    assert n["network_resource"] == "dns" and n["network_path"] == "ns1.example.com" and n["tracking"] is True


# ---------------- network resources ----------------
def test_network_resources_are_types(client):
    resources = client.get("/api/network-resources").json()["network_resources"]
    assert resources == ["dns", "server", "shared_folder", "web"]


def test_network_resource_migration_remaps_legacy_values(data_dir):
    """Legacy node resource values and dataset-template payload resource values
    are remapped onto the current Network Resource types on startup (idempotent)."""
    from app import db

    db.init_db()  # create the schema
    with db.db_conn() as conn:
        for nid, res in (("a", "dns_alias"), ("b", "file_share"), ("c", "vm")):
            conn.execute(
                "INSERT INTO nodes (id,label,network_resource,created_at,updated_at) VALUES (?,?,?,?,?)",
                (nid, nid, res, "x", "x"))
        conn.execute(
            "INSERT INTO templates (name,template_type,payload,created_at) VALUES (?,?,?,?)",
            ("t-legacy", "dataset", json.dumps({"name": "t", "network_resource": "dns_alias"}), "x"))

    db.init_db()  # startup migration

    with db.db_conn() as conn:
        got = dict(conn.execute("SELECT id, network_resource FROM nodes").fetchall())
        assert got == {"a": "dns", "b": "shared_folder", "c": "server"}
        payload = json.loads(conn.execute(
            "SELECT payload FROM templates WHERE name='t-legacy'").fetchone()["payload"])
        assert payload["network_resource"] == "dns"

    db.init_db()  # idempotent: a second run changes nothing
    with db.db_conn() as conn:
        assert dict(conn.execute("SELECT id, network_resource FROM nodes").fetchall()) == got


# ---------------- per-node collection queue ----------------
def test_run_node_queues_enabled_datasets(client):
    _mk_node(client)
    body = {"name": "d1", "method": "bash", "script": "echo v=1"}
    ds1 = client.post("/api/nodes/n1/datasets", json=body).json()
    ds2 = client.post("/api/nodes/n1/datasets", json=body).json()
    # Disable the second dataset (patch replaces the whole body).
    client.patch(f"/api/datasets/{ds2['id']}", json={**body, "name": "d2", "enabled": False})

    r = client.post("/api/nodes/n1/run")
    assert r.status_code == 200
    # only the enabled dataset is collected, in-process (started) even with the
    # scheduler disabled.
    assert r.json() == {"ok": True, "queued": 1, "started": 1}


def test_run_node_missing(client):
    assert client.post("/api/nodes/nope/run").status_code == 404


def test_settings_smtp_password_hidden(client):
    client.put("/api/settings", json={"values": {"smtp_password": "pw"}})
    assert client.get("/api/settings").json()["smtp_password"] == "***"


def test_theme_setting_default_and_roundtrip(client):
    # Seeded default is light.
    assert client.get("/api/settings").json()["theme"] == "light"
    # Round-trips through PUT/GET.
    client.put("/api/settings", json={"values": {"theme": "dark"}})
    assert client.get("/api/settings").json()["theme"] == "dark"
    client.put("/api/settings", json={"values": {"theme": "light"}})
    assert client.get("/api/settings").json()["theme"] == "light"


def test_snapshot_includes_theme(client):
    assert client.get("/api/snapshot").json()["theme"] == "light"
    client.put("/api/settings", json={"values": {"theme": "dark"}})
    assert client.get("/api/snapshot").json()["theme"] == "dark"
    client.put("/api/settings", json={"values": {"theme": "light"}})


# ---------------- email alert defaults ----------------
def test_alert_default_settings_seeded(client):
    s = client.get("/api/settings").json()
    assert s["alert_default_recipients"] == ""
    assert "{{node.label}}" in s["alert_default_subject"] and "{{status}}" in s["alert_default_subject"]
    assert "{{metric.name}}" in s["alert_default_body"] and "{{status}}" in s["alert_default_body"]
    assert s["alert_default_cooldown_s"] == "3600"


def test_alert_default_settings_roundtrip(client):
    vals = {"alert_default_recipients": "ops@x.io", "alert_default_subject": "S: {{status}}",
            "alert_default_body": "B: {{metric.name}}", "alert_default_cooldown_s": "60"}
    client.put("/api/settings", json={"values": vals})
    s = client.get("/api/settings").json()
    assert all(s[k] == v for k, v in vals.items())


def _mk_alert_metric(client, subject="", body="", recipients="ops@x.io"):
    """A metric whose rule fires critical, with an enabled alert (blank subject/body)."""
    _mk_node(client, "n1", "web01")
    ds = client.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo rtt=9999"}).json()
    m = client.post(f"/api/datasets/{ds['id']}/metrics", json={"name": "rtt", "regex": r"rtt=(\d+)"}).json()
    critical = next(l for l in client.get("/api/status-levels").json()["status_levels"] if l["name"] == "critical")
    client.post(f"/api/metrics/{m['id']}/rules", json={"op": "gt", "value": "100", "status_level_id": critical["id"]})
    client.put(f"/api/metrics/{m['id']}/alert",
               json={"enabled": True, "recipients": recipients, "subject": subject,
                     "body": body, "cooldown_s": 0, "only_on_change": False})
    return ds["id"]


def test_alert_blank_subject_body_uses_global_defaults(client, monkeypatch):
    from app import mailer
    sent = []
    monkeypatch.setattr(mailer, "send_email",
                        lambda conn, r, s, b: sent.append((r, s, b)) or True)
    client.put("/api/settings", json={"values": {
        "alert_default_subject": "{{status}}: {{node.label}} / {{metric.name}}",
        "alert_default_body": "{{node.label}} — metric {{metric.name}} is {{status}} (value={{value}})."}})
    ds_id = _mk_alert_metric(client)
    client.post(f"/api/datasets/{ds_id}/run")
    assert sent, "expected an alert to be sent"
    _, subj, body = sent[-1]
    assert subj == "critical: web01 / rtt"
    assert body == "web01 — metric rtt is critical (value=9999)."


def test_alert_own_subject_body_wins_over_defaults(client, monkeypatch):
    from app import mailer
    sent = []
    monkeypatch.setattr(mailer, "send_email",
                        lambda conn, r, s, b: sent.append((r, s, b)) or True)
    client.put("/api/settings", json={"values": {"alert_default_subject": "GLOBAL", "alert_default_body": "GLOBAL"}})
    ds_id = _mk_alert_metric(client, subject="Own {{status}}", body="own body {{value}}")
    client.post(f"/api/datasets/{ds_id}/run")
    assert sent
    _, subj, body = sent[-1]
    assert subj == "Own critical" and body == "own body 9999"


# ---------------- import / export ----------------
import base64
import contextlib
import io
import zipfile


@contextlib.contextmanager
def _instance(monkeypatch, tmp_path, name):
    """A fully isolated app instance (own data dir + keyfile) for cross-instance
    import/export tests. The db module paths are global, so instances are used
    one at a time (enter, exercise, exit) rather than concurrently."""
    from fastapi.testclient import TestClient
    from app import main, db, crypto
    d = tmp_path / name
    monkeypatch.setenv("SM_DISABLE_SCHEDULER", "1")
    monkeypatch.setattr(db, "DATA_DIR", d)
    monkeypatch.setattr(db, "DB_PATH", d / "monitor.db")
    monkeypatch.setattr(db, "KEY_PATH", d / "secret.key")
    crypto._box = None
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        crypto._box = None


def test_export_zip_contents(client):
    _mk_node(client)
    client.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "s"})
    zbytes = client.get("/api/export").content
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    assert set(zf.namelist()) == {"monitor.db", "graph.json", "manifest.json"}
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["includes_secret_key"] is False
    assert manifest["credential_count"] == 1
    graph = json.loads(zf.read("graph.json"))
    assert any(n["id"] == "n1" for n in graph["nodes"])


def test_export_import_roundtrip_replaces_data(monkeypatch, tmp_path):
    # Instance A: build data + a credential, then export.
    with _instance(monkeypatch, tmp_path, "A") as a:
        _mk_node(a, "n1", "web01")
        a.post("/api/nodes/n1/datasets", json={"name": "d", "method": "bash", "script": "echo v=1"})
        a.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "s3cret"})
        a.put("/api/settings", json={"values": {"mail_from": "a@example.com"}})
        zbytes = a.get("/api/export").content

    # Instance B: fresh, then import A's archive.
    with _instance(monkeypatch, tmp_path, "B") as b:
        assert b.get("/api/snapshot").json()["nodes"] == []
        r = b.post("/api/import", json={"zip_b64": base64.b64encode(zbytes).decode()})
        assert r.status_code == 200, r.text
        snap = b.get("/api/snapshot").json()
        assert [n["id"] for n in snap["nodes"]] == ["n1"]
        assert snap["nodes"][0]["label"] == "web01"
        assert snap["nodes"][0]["datasets"][0]["name"] == "d"
        assert b.get("/api/settings").json()["mail_from"] == "a@example.com"
        assert r.json()["nodes"] == 1 and r.json()["datasets"] == 1


def test_import_flags_credentials_needing_rekey(monkeypatch, tmp_path):
    # A encrypts a credential with its own key; B (different key) can't decrypt.
    with _instance(monkeypatch, tmp_path, "A") as a:
        a.post("/api/credentials", json={"name": "svc", "username": "u", "secret": "s3cret"})
        zbytes = a.get("/api/export").content

    with _instance(monkeypatch, tmp_path, "B") as b:
        r = b.post("/api/import", json={"zip_b64": base64.b64encode(zbytes).decode()})
        assert r.status_code == 200
        assert r.json()["credentials_needing_rekey"][0]["name"] == "svc"
        creds = b.get("/api/credentials").json()["credentials"]
        assert creds[0]["name"] == "svc" and creds[0]["needs_rekey"] == 1
        # Re-entering the secret re-encrypts with B's key and clears the flag.
        cid = creds[0]["id"]
        b.put(f"/api/credentials/{cid}", json={"name": "svc", "username": "u", "secret": "newsecret"})
        assert b.get("/api/credentials").json()["credentials"][0]["needs_rekey"] == 0


def test_import_rejects_bad_input(client):
    # Not base64.
    assert client.post("/api/import", json={"zip_b64": "!!!not base64!!!"}).status_code == 400
    # Valid base64 but not a zip.
    assert client.post("/api/import", json={"zip_b64": base64.b64encode(b"hello").decode()}).status_code == 400
    # A zip without monitor.db.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    assert client.post("/api/import", json={"zip_b64": base64.b64encode(buf.getvalue()).decode()}).status_code == 400


def test_import_backs_up_current_db(monkeypatch, tmp_path):
    with _instance(monkeypatch, tmp_path, "A") as a:
        _mk_node(a, "n1", "web01")
        zbytes = a.get("/api/export").content
    with _instance(monkeypatch, tmp_path, "B") as b:
        _mk_node(b, "zzz", "old")  # B has its own data before import
        b.post("/api/import", json={"zip_b64": base64.b64encode(zbytes).decode()})
        backups = list((tmp_path / "B").glob("monitor.pre-import-*.db"))
        assert backups, "expected a pre-import backup of the previous database"


# --- static bundles: /admin (full) and /dashboard (read-only) ---

def test_root_redirects_to_dashboard(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard/"


def test_admin_bundle_served(client):
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "js/app.js" in r.text  # full app: right dock + editor modules
    assert client.get("/admin/app.css").status_code == 200
    assert client.get("/admin/js/panel.js").status_code == 200
    assert client.get("/admin/vendor/nodegraph.js").status_code == 200


def test_dashboard_bundle_served(client):
    r = client.get("/dashboard/")
    assert r.status_code == 200
    # Read-only: has the details dock but no admin editing modules.
    assert client.get("/dashboard/js/dock.js").status_code == 200
    assert "editor.js" not in r.text
    assert client.get("/dashboard/app.css").status_code == 200
    assert client.get("/dashboard/js/app.js").status_code == 200
    assert client.get("/dashboard/vendor/nodegraph.js").status_code == 200


def test_api_reachable_under_bundle_prefixes(client):
    _mk_node(client, node_id="px1")
    for prefix in ("/admin", "/dashboard"):
        r = client.get(f"{prefix}/api/snapshot")
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["nodes"]] == ["px1"]
    # PUT through the prefix works too (admin layout saves).
    assert client.put("/admin/api/graph", json={"model": {
        "version": 1, "nodes": [], "edges": [], "groups": []}}).status_code == 200
    assert client.get("/api/snapshot").json()["nodes"] == []


# ---------------- read-only SQL query bar + saved queries ----------------
def _seed_metric(client, node_id="n1", metric="rtt", value="42"):
    """A node with one dataset + metric whose latest value is collected."""
    ds = client.post(f"/api/nodes/{node_id}/datasets", json={
        "name": "d", "method": "bash", "script": f"echo {metric}={value}"}).json()
    client.post(f"/api/datasets/{ds['id']}/metrics",
                json={"name": metric, "regex": rf"{metric}=(\d+)", "group_index": 1})
    client.post(f"/api/datasets/{ds['id']}/run")
    return ds


def test_query_returns_node_ids(client):
    _mk_nodes(client, [("n1", "web01"), ("n2", "db01")])
    r = client.post("/api/query", json={"sql": "SELECT id FROM nodes_wide ORDER BY label"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ids"] == ["n2", "n1"] and body["count"] == 2


def test_query_filters_rows_by_status(client):
    _mk_node(client)
    _seed_metric(client)
    r = client.post("/api/query",
                    json={"sql": "SELECT id FROM nodes_wide WHERE status = 'unknown'"}).json()
    assert r["ids"] == ["n1"]
    r2 = client.post("/api/query",
                     json={"sql": "SELECT id FROM nodes_wide WHERE status = 'critical'"}).json()
    assert r2["ids"] == []


def test_query_metric_column_with_na(client):
    _mk_nodes(client, [("n1", "web01"), ("n2", "db01")])
    _seed_metric(client, "n1", "rtt", "42")
    # Exact and lowercase metric names both resolve to the same column.
    r = client.post("/api/query", json={"sql": 'SELECT id, "RTT" FROM nodes_wide ORDER BY id'})
    assert r.status_code == 200 and r.json()["columns"] == ["id", "RTT"]
    # n1 has the metric (a real value); n2 lacks it and shows the 'N/A' default.
    have = client.post("/api/query",
                       json={"sql": 'SELECT id FROM nodes_wide WHERE "rtt" <> \'N/A\''}).json()
    assert have["ids"] == ["n1"]
    missing = client.post("/api/query",
                          json={"sql": "SELECT id FROM nodes_wide WHERE rtt = 'N/A'"}).json()
    assert missing["ids"] == ["n2"]


def test_query_columns_lists_node_fields_and_metrics(client):
    _mk_node(client)
    _seed_metric(client, metric="cpu")
    cols = client.get("/api/query/columns").json()["columns"]
    for c in ("id", "label", "network_resource", "network_path", "tracking", "status", "datasets"):
        assert c in cols
    assert "cpu" in cols


def test_query_auto_injects_from_nodes_wide(client):
    _mk_nodes(client, [("n1", "web01"), ("n2", "db01")])
    # No FROM clause at all — the visible table is assumed.
    r = client.post("/api/query", json={"sql": "SELECT id WHERE status = 'unknown' ORDER BY label"})
    assert r.status_code == 200, r.text
    assert r.json()["ids"] == ["n2", "n1"]
    # A WHERE-less query still works.
    r2 = client.post("/api/query", json={"sql": "SELECT id"})
    assert r2.status_code == 200 and sorted(r2.json()["ids"]) == ["n1", "n2"]


def test_query_first_column_resolves_label_to_id(client):
    _mk_nodes(client, [("n1", "web01"), ("n2", "db01")])
    # The visible first column is Label; selecting it still filters the right rows.
    r = client.post("/api/query", json={"sql": "SELECT label WHERE status = 'unknown' ORDER BY label"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["ids"]) == ["n1", "n2"]
    # A specific label resolves to exactly that node's id.
    r2 = client.post("/api/query", json={"sql": "SELECT label WHERE label = 'web01'"}).json()
    assert r2["ids"] == ["n1"]


def test_query_returns_selected_rows(client):
    _mk_nodes(client, [("n1", "web01"), ("n2", "db01")])
    # The table shows exactly what was queried: one row per record, one cell per
    # selected column — not the full node grid.
    r = client.post("/api/query", json={"sql": "SELECT label WHERE label = 'web01'"})
    body = r.json()
    assert body["columns"] == ["label"]
    assert body["rows"] == [{"label": "web01"}]
    # Multiple columns come back as dict rows keyed by the selected expressions.
    r2 = client.post("/api/query", json={"sql": "SELECT id, status ORDER BY id"}).json()
    assert r2["columns"] == ["id", "status"]
    assert [row["id"] for row in r2["rows"]] == ["n1", "n2"]


def test_query_explicit_from_not_double_injected(client):
    _mk_node(client)
    # An explicit FROM (or subquery) is left untouched — no second FROM injected.
    r = client.post("/api/query", json={"sql": "SELECT id FROM nodes_wide WHERE 1=1"})
    assert r.status_code == 200, r.text
    assert r.json()["ids"] == ["n1"]


def test_query_rejects_writes_and_multi_statement(client):
    _mk_node(client)
    bad = [
        "DELETE FROM nodes_wide",
        "DROP TABLE nodes",
        "CREATE TABLE evil (x)",
        "UPDATE nodes SET label='x'",
        "INSERT INTO nodes (id,label,created_at,updated_at) VALUES ('z','z','x','x')",
        "SELECT 1; SELECT 2",
        "ATTACH DATABASE '/tmp/evil.db' AS e",
        "SELECT id FROM nodes_wide WHERE 1=1 UNION SELECT load_extension('x')",
    ]
    for sql in bad:
        r = client.post("/api/query", json={"sql": sql})
        assert r.status_code == 400, f"{sql!r} -> {r.status_code} {r.text[:120]}"
    # The database is untouched.
    assert [n["id"] for n in client.get("/api/snapshot").json()["nodes"]] == ["n1"]


def test_query_rejects_empty(client):
    assert client.post("/api/query", json={"sql": "   "}).status_code == 400


def test_saved_queries_crud_roundtrip(client):
    _mk_node(client)
    r = client.post("/api/saved-queries", json={"name": "All nodes", "sql": "SELECT id FROM nodes_wide"})
    assert r.status_code == 200, r.text
    qid = r.json()["id"]
    listed = client.get("/api/saved-queries").json()["saved_queries"]
    assert [q["name"] for q in listed] == ["All nodes"]
    assert listed[0]["sql"] == "SELECT id FROM nodes_wide"
    # Duplicate name -> 409.
    assert client.post("/api/saved-queries",
                       json={"name": "All nodes", "sql": "SELECT 1"}).status_code == 409
    # Delete + 404 on second delete.
    assert client.delete(f"/api/saved-queries/{qid}").status_code == 200
    assert client.delete(f"/api/saved-queries/{qid}").status_code == 404
    assert client.get("/api/saved-queries").json()["saved_queries"] == []


def test_saved_query_validated_on_save(client):
    # A broken query is refused and never persisted.
    r = client.post("/api/saved-queries", json={"name": "bad", "sql": "SELECT nope FROM nope"})
    assert r.status_code == 400
    assert client.get("/api/saved-queries").json()["saved_queries"] == []
    # A write-shaped query is refused too.
    r2 = client.post("/api/saved-queries", json={"name": "bad2", "sql": "DELETE FROM nodes"})
    assert r2.status_code == 400
    # Empty name refused.
    assert client.post("/api/saved-queries",
                       json={"name": "  ", "sql": "SELECT id FROM nodes_wide"}).status_code == 400
