# Uptracker

A visual network-object status monitor: a node-graph editor (powered by
[`nodegraph`](admin/vendor/nodegraph.js)) plus a right-docked configuration panel, a
FastAPI backend, and a SQLite store. Nodes represent servers, VMs, DNS aliases,
and file shares. Each node's colour shows its status; ports and edges are fully
editable in the admin editor (and shown, read-only, on the dashboard).

## Concepts

- **Node** — a network object. Opt in with *Tracking*.
- **Dataset** — a retrieval job on a schedule (methods: `bash`, `cmd`,
  `powershell`). Runs server-side.
- **Metric** — a named variable extracted from the dataset output via a regex
  capture group, with a type cast.
- **Rule** — maps a metric value to a status level (`>`, `<`, `contains`,
  `missing`, …). The worst matching status wins.
- **Status** — fixed levels (good / warning / critical / unknown). A node's
  status is the worst across its enabled datasets.
- **Alert** — optional per-metric email when a non-default status is reached.
- **Template** — reusable dataset (with metrics/rules); apply to other nodes.
- **Credential** — encrypted at rest (Fernet keyfile). Secrets are write-only
  and must be re-entered whenever a dataset's script/method/credential changes.

Only the latest value per metric is stored — no historical time series. The UI
polls `/api/snapshot` on an interval.

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
| `/dashboard` | **Read-only** view: node graph (editing disabled) + status table. No right panel. Each node has a refresh button (⟳) that queues all of its enabled datasets for collection. Root `/` redirects here. |
| `/admin`     | Full editor: graph editing, right dock (datasets/metrics/rules, templates, credentials, settings). |

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
**Application Request Routing (ARR)**, then enable *Proxy* in the ARR feature.

1. Run Uptracker on the server (e.g. via NSSM or Task Scheduler so it starts at
   boot): `uvicorn app.main:app --host 127.0.0.1 --port 8090`.
2. In IIS, add a **URL Rewrite** inbound rule per path to forward to the app:

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

3. Enable **Basic Authentication** for the site and add an *Authorization* rule
   restricting `/admin` to your users. Terminate TLS with an HTTPS binding.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

```
app/        backend (db, crypto, parser, rules, executor, scheduler, mailer, templates, query, main)
admin/      full frontend bundle (index.html, app.css, js/*, vendor/nodegraph.js)
dashboard/  read-only frontend bundle (graph + table only, no dock/editor modules)
tests/      pytest suite
data/       SQLite db + keyfile (gitignored — never commit)
```

Each frontend bundle is self-contained and carries its own copy of the
`nodegraph` library under `vendor/`. The two vendor copies are kept identical —
if you change the library, apply the change to both `admin/vendor/nodegraph.js`
and `dashboard/vendor/nodegraph.js`.

## Notes

- Scripts execute locally on the server host; credentials are injected into the
  child process env (`SM_USER`, `SM_SECRET`) and never logged.
- SMTP is configured in Settings; without a host, alerts are logged instead of
  sent.
- The graph layout is the source of truth for node positions/labels; removing a
  node from the canvas deletes its datasets.
- Environment variables: `PORT` (listen port), `SM_DATA_DIR` (data directory),
  `SM_DISABLE_SCHEDULER=1` (run without the background scheduler).
