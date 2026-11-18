# Diff Travel Premium PDF API

The premium API lives in `premium/` on this repository's `main` branch.
The legacy API starts from the repository root and serves `POST /generate-pdf/`.
Premium has its own ASGI application, dependency set, database, volumes and containers.
It serves `POST /v2/documents` and the document review/download routes below `/v2`.

## Dokploy deployment

The Portuguese [deployment runbook](DEPLOYMENT.md) covers setup, verification,
Bubble handoff, backups and rollback.

Create a Compose service for premium in the same server/project as the legacy application.
Use this repository and branch `main`. The compose files are
`premium/docker-compose.yml` and `premium/docker-compose.dokploy.yml`.
The second file joins the API service to the existing external `dokploy-network`.
Build context resolves to `premium/`.

The stack contains `api`, `worker` and `db`, plus dedicated `documents` and `database`
named volumes. Route a separate premium HTTPS hostname to service `api`, internal
port `8000`. The loopback-only host port is configurable with `PDF_HOST_PORT`.
Check that the chosen host port is available before deployment.

Generate unique deployment credentials with `python scripts/setup_env.py --docker`
in a private working copy. Copy the relevant values to Dokploy's Environment editor.
Apply the settings from `.env.dokploy.example`, including `PDF_RENDER_MODE=html_template`,
`PDF_PIPELINE_VERSION=canonical_v1` and the actual premium `PDF_PUBLIC_BASE_URL`.
The OpenAI key belongs in that private environment.

Before integrating Bubble, configure `PDF_LEGACY_PDF_HOSTS` with the exact allowed
standard-PDF hostname(s) for the tenant. Set `PDF_REQUIRE_LEGACY_PDF_URL=true` when
every call includes the corresponding standard PDF URL.

The Chromium seccomp profile is copied from Microsoft's Playwright v1.64.1
`utils/docker/seccomp_profile.json`. Its license accompanies the profile.
The container runs as UID 10001 with Chromium sandboxing enabled.

## Validation

From `premium/`, install `requirements-dev.txt`, install the Playwright Chromium
browser and run `python -m pytest -q`.
`python scripts/smoke_canonical_http.py --browser-channel chromium` exercises real
HTTP and a separate worker with an explicitly synthetic provider double.

Health endpoints are `/healthz` and `/readyz`. Monitor the worker heartbeat as well.
For the legacy contract smoke, run `scripts/smoke_legacy_http.py --base-url <legacy-origin>`.
The smoke uses synthetic input and checks the two-page PDF response.

The premium source in this release comes from the independent local premium
project. Deployment contains code, brand assets and synthetic test fixtures.
