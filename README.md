# Uptracker

A visual network-object status monitor: a node-graph editor (powered by
[`nodegraph`](admin/vendor/nodegraph.js)) plus a right-docked configuration panel, a
FastAPI backend, and a SQLite store. Nodes represent servers, VMs, DNS aliases,
and file shares. Each node's colour shows its status; ports and edges are fully
editable in the admin editor (and shown, read-only, on the dashboard).

## Concepts

- **Node** — a network object. Opt in with *Tracking*.
- **Dataset** — a retrieval job on a schedule (methods: `bash`, `cmd`,
  `powershell`). Runs server-side. Scheduling is opt-out: **Not scheduled**
  (`interval_s = 0`) makes an on-demand dataset the timer never runs — collect
  it with *Run*. A timeout of `0` disables the subprocess time limit entirely,
  for long-running on-demand scripts. Datasets can also be **hidden** from the
  dashboard (see Deployment paths).
- **Metric** — a named variable extracted from the dataset output via a regex
  capture group, with a type cast.
- **Rule** — maps a metric value to a status level (`>`, `<`, `contains`,
  `missing`, …). The worst matching status wins.
- **Status** — fixed levels (good / warning / critical / unknown). A node's
  status is the worst across its visible, scheduled datasets.
- **Alert** — optional per-metric email when a non-default status is reached.
- **Template** — reusable dataset (with metrics/rules); apply to other nodes.
- **Credential** — encrypted at rest (Fernet keyfile). Secrets are write-only
  and must be re-entered whenever a dataset's script/method/credential changes.

Only the latest value per metric is stored — no historical time series. The UI
polls `/api/snapshot` on an interval. Datasets flagged **Hide on dashboard** in
the admin are omitted from the dashboard's snapshot entirely (graph dock, status
table, and node refresh); they still collect on schedule and contribute nothing
to the node status rollup. Admin and the bare API always see every dataset.

## Requirements

- Python 3.10+
- A POSIX shell or WSL for running collection scripts (`bash`). On Windows, the
  `cmd` and `powershell` methods run natively.

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run (serves on http://localhost:8090; override with PORT=xxxx)
./run.sh                           # Windows: uvicorn app.main:app --port 8090
```

First run creates `data/monitor.db` and `data/secret.key` (permissions `0600`).
Both live under `data/`, which is gitignored — never commit them.

## Deployment paths

The app serves two self-contained static bundles from the same backend/API:

| Path         | What                                                                 |
|--------------|----------------------------------------------------------------------|
| `/dashboard` | **Read-only** view: node graph (editing disabled) + status table. No right panel. Each node has a refresh button (⟳) that queues all of its scheduled datasets for collection (hidden and on-demand datasets are skipped). Root `/` redirects here. |
| `/admin`     | Full editor: graph editing, right dock (datasets/metrics/rules, templates, credentials, settings). |
| `/designer`  | Standalone [nodegraph.js](admin/vendor/nodegraph.js) playground: a fully editable empty canvas with no Uptracker data, API usage, or persistence. The library's context menu (double-click the canvas) is the only interface — it covers Export/Import JSON, PDF export, fit/reset view, and theming. Holds no data, so it needs no proxy protection. |

The application itself has **no built-in authentication** — lock down `/admin`
at the web server in front of it. Uptracker listens on `127.0.0.1:8090` (or
`0.0.0.0:8090` via `run.sh`); always terminate TLS and enforce access control at
the reverse proxy, not on the app port.

Both bundles call the API with relative URLs, so each also works under a path
prefix (`/admin/api/...`, `/dashboard/api/...`) — handy for proxies and port
forwarding.

### nginx

```nginx
# Protect the admin editor with HTTP basic auth.
location /admin {
    auth_basic           "Uptracker admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /dashboard {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Apache (mod_proxy)

Enable the modules once: `a2enmod proxy proxy_http headers auth_basic`.

```apache
# Protect the admin editor with HTTP basic auth.
<Location /admin>
    AuthType Basic
    AuthName "Uptracker admin"
    AuthUserFile /etc/apache2/.htpasswd
    Require valid-user
</Location>

ProxyPreserveHost On
ProxyPass /admin     http://127.0.0.1:8090/admin
ProxyPassReverse /admin http://127.0.0.1:8090/admin
ProxyPass /dashboard     http://127.0.0.1:8090/dashboard
ProxyPassReverse /dashboard http://127.0.0.1:8090/dashboard
```

### IIS (ARR + URL Rewrite)

Uptracker is a Python/uvicorn app, so on Windows it runs as a background process
and IIS fronts it as a reverse proxy. Prerequisites: install **URL Rewrite** and
**Application Request Routing (ARR)** (both via the Web Platform Installer).

1. Run Uptracker on the server (e.g. via NSSM or Task Scheduler so it starts at
   boot): `uvicorn app.main:app --host 127.0.0.1 --port 8090`.
2. **Enable the ARR proxy — this is required and off by default.** Select the
   *server* node (not the site) → **Application Request Routing Cache** →
   right-click → **Server Proxy Settings** → check **Enable proxy** → Apply.
   Without this, IIS has no handler for the path and returns **404** even though
   uvicorn answers fine on `localhost:8090`.
3. In IIS, add a **URL Rewrite** inbound rule per path to forward to the app.
   Forward the **full path** with `{R:1}` so `/admin/api/...` and
   `/dashboard/api/...` reach uvicorn intact (the app's prefix middleware and
   relative-URL `<base>` fixer depend on this):

```xml
<system.webServer>
  <rewrite>
    <rules>
      <rule name="Uptracker dashboard" stopProcessing="true">
        <match url="^dashboard(/.*)?$" />
        <action type="Rewrite" url="http://127.0.0.1:8090/dashboard{R:1}" />
      </rule>
      <rule name="Uptracker admin" stopProcessing="true">
        <match url="^admin(/.*)?$" />
        <action type="Rewrite" url="http://127.0.0.1:8090/admin{R:1}" />
      </rule>
    </rules>
  </rewrite>
  <!-- Protect /admin with IIS Basic Authentication (enable the feature and
       configure an authorization rule scoping to the /admin path). -->
</system.webServer>
```

4. Enable **Basic Authentication** for the site and add an *Authorization* rule
   restricting `/admin` to your users. Terminate TLS with an HTTPS binding.

#### Troubleshooting IIS 404s

If `localhost:8090` works directly but the proxied URL returns 404, the request
isn't reaching uvicorn — it's an ARR/rewrite config issue, not an app bug.
Isolate it by comparing direct vs. proxied responses:

```bash
# Backend directly (expect 200 / 307):
curl -i http://localhost:8090/dashboard/
curl -i http://localhost:8090/admin/api/snapshot
# Through IIS (site binding, e.g. port 80/443):
curl -i http://localhost/dashboard/
curl -i http://localhost/admin/api/snapshot
```

- Direct works, proxied **404** → ARR proxy not enabled (step 2) or the rewrite
  rule isn't matching. Confirm **Enable proxy** is checked and the rule is on
  the correct *site* (rules defined per-site only apply to that site).
- Proxied **500.x / "URL Rewrite Module Error"** → a server variable such as
  `HTTP_X_FORWARDED_HOST` isn't delegatable; add it under ARR → **Server
  Variables** with *Allowed = True*.
- Page loads but only `/admin/api/...` (or `/dashboard/api/...`) 404s → the rule
  is mangling the path; make sure `{R:1}` forwards the full remaining path.
- Proxying under a sub-path (e.g. `https://host/uptracker/`): match and strip the
  prefix, e.g. `<match url="^uptracker/(dashboard/.*)$" />` →
  `url="http://127.0.0.1:8090/{R:1}"`.

## Rule operators

A rule compares a metric's parsed value against its *rule value* and assigns a
status level when it matches. The worst matching status wins; no match → unknown.

| Op | UI label | Use case | Rule value example |
|----|----------|----------|--------------------|
| `gt` | `>` | Threshold exceeded — disk %, temperature, latency | `90` |
| `lt` | `<` | Value below a floor — free space, remaining days | `10` |
| `gte` | `≥` | Threshold reached (inclusive) | `80` |
| `lte` | `≤` | Within a ceiling (inclusive) | `80` |
| `eq` | `=` | Exact match — state strings or numbers; compares leading numbers when both sides are numeric (`"85 %"` = `85%`) | `85%` |
| `ne` | `≠` | Anything except a specific value | `DOWN` |
| `contains` | `contains` | Substring in free text — service state, OS caption | `Not Responding` |
| `regex` | `regex` | Full pattern match on the captured text | `^CRIT\|WARN$` |
| `missing` | `is missing` | Metric absent — script failed or key gone from output (no rule value) | — |
| `present` | `is present` | Metric collected at all, whatever the value (no rule value) | — |
| `is` | `is` | Boolean state — ping True/False, service Up/Down. Words `1/true/yes/on/up/ok` vs `0/false/no/off/down`; a non-zero number counts as true | `True` |
| `is_not` | `is not` | Negated boolean — "not up" | `False` |
| `between` | `between` | Warning band, e.g. 80–90 % before critical; accepts `min,max` or `min max` | `80,90` |

Notes:

- Numeric ops (`gt/lt/gte/lte/between`) fall back to the value's leading
  numeric prefix, so unit-suffixed text like `"85 %"` works without a `float` cast.
- Comparison errors (invalid regex, non-numeric threshold, malformed range) are
  reported per rule in the dock terminal when you press **Test rules**; during
  scheduled collection they count as no-match.
- Reference implementation: `app/rules.py` (`compare()` dispatch table), mirrored
  by `admin/js/rulesEngine.js`.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

```
app/        backend (db, crypto, parser, rules, executor, scheduler, mailer, templates, query, main)
admin/      full frontend bundle (index.html, app.css, js/*, vendor/nodegraph.js)
dashboard/  read-only frontend bundle (graph + table only, no dock/editor modules)
designer/   standalone nodegraph playground (no API usage, no persistence)
tests/      pytest suite
data/       SQLite db + keyfile (gitignored — never commit)
```

Each frontend bundle is self-contained and carries its own copy of the
`nodegraph` library under `vendor/`. The three vendor copies are kept identical —
if you change the library, apply the change to `admin/vendor/nodegraph.js`,
`dashboard/vendor/nodegraph.js`, and `designer/vendor/nodegraph.js`.

## Notes

- Scripts execute locally on the server host; credentials are injected into the
  child process env (`SM_USER`, `SM_SECRET`) and never logged.
- SMTP is configured in Settings; without a host, alerts are logged instead of
  sent.
- The graph layout is the source of truth for node positions/labels; removing a
  node from the canvas deletes its datasets.
- Environment variables: `PORT` (listen port), `SM_DATA_DIR` (data directory),
  `SM_DISABLE_SCHEDULER=1` (run without the background scheduler).
