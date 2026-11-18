# Diff Travel Premium PDF API

Premium code lives in `premium/` on this repository's `main` branch.
The selected deployment runs both APIs and the premium worker inside the **same
existing Dokploy service**, sharing its public hostname and port.

| Public route | Application |
| --- | --- |
| `POST /generate-pdf/` | Original API, unchanged multipart contract |
| `/docs`, `/openapi.json` | Original API documentation |
| `POST /v2/documents` and `/v2/...` | Premium upload, review and download |
| `/premium/openapi.json` | Premium API schema |
| `/healthz`, `/readyz` | Premium health and dependency readiness |

The shared image has independent virtual environments for the original pinned
dependencies and the premium dependencies. Nginx routes one public port to the
two loopback-only ASGI applications. Supervisor runs/restarts these processes and
the premium worker; Tini reaps subprocesses. All processes run as UID 10001.
The shared-only legacy entry adapter fixes internal upload filenames before
delegating to the original handler, protecting premium storage from path traversal
without changing the original source or HTTP schema.

Premium activation is explicit: `PDF_PREMIUM_ENABLED=true`. With activation off,
the image serves the legacy API only. If premium startup validation fails, the
gateway continues to serve the original API and premium routes return HTTP 503.
Premium health checks remain unhealthy so the deployment cannot be mistaken for
a successful premium activation.

The first shared deployment uses SQLite WAL and document files on one named,
persistent volume mounted at `/app/var`. A process-held file lock prevents two
premium instances from sharing it. Use one replica, node placement matching the
volume, and stop-first updates/rollbacks. A full service restart affects both APIs.

## Synthetic validation

Continuous integration validates standalone and shared runtimes with an explicit
synthetic provider, without real keys or customer data.

The shared smoke verifies the old two-page PDF contract, actual premium PDF
generation, authentication, worker crash recovery, persisted artifacts after a
container restart, disabled activation and invalid premium startup configuration.
The test provider records exactly one synthetic response and zero real AI calls.

The independent premium Dockerfile/Compose files remain available for diagnostics
and earlier validation; the current hosting procedure uses the shared Dockerfile.
