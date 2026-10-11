# Containerized Magpie Core

The container runtime is a parallel product-development path. It does not
replace or modify the working Raspberry Pi systemd installation.

## Boundary

The central Magpie service owns:

- the storage graph and inventory API;
- agent planning, search, and placement decisions;
- photos, labels, rules, and review workflows;
- the web, mobile-web, and kiosk pages.

The Raspberry Pi remains the edge controller for microphones, speakers,
screens, WLED devices, and locally connected printers. The container uses the
`none` light backend so it can run without Raspberry Pi libraries or direct
hardware access. The authenticated edge command queue connects central light
actions to a Pi without exposing WLED or a Supabase key outside the LAN; see
`docs/edge-controller.md`.

## Run Beside The Pi Service

The Compose mapping deliberately exposes port `8766`, leaving the existing Pi
service on `8765` unchanged. It binds to loopback by default because the local
container has no human authentication and includes inventory mutation
endpoints. This keeps a development stack off the LAN unless the operator
explicitly chooses otherwise.

```sh
docker compose up --build -d
curl http://127.0.0.1:8766/health
curl http://127.0.0.1:8766/ready
curl http://127.0.0.1:8766/build
```

`/health` is the liveness endpoint: it proves the Magpie process is serving
requests and reports its configured runtime. `/ready` additionally performs a
bounded query against the active SQLite or Supabase location store and an
ephemeral write-and-delete probe against the operational state directory.
Compose and hosted promotion use `/ready`, so a running service with an
unreachable storage backend or unusable state volume is not promoted or
reported healthy.

Release automation should set `MAGPIE_BUILD_COMMIT`, `MAGPIE_BUILD_BRANCH`, and
`MAGPIE_BUILT_AT` before building. Local images report `container-dev` so the
build endpoint is still explicit rather than presenting an empty commit.
The image also pins its multi-architecture Python base by digest and installs
the exact dependency versions in `requirements-container.lock`. Update that
lock intentionally with Python 3.11 resolution, run the full test suite, and
build both supported architectures before accepting dependency changes. The
Docker context excludes local environment files, `secrets.env`, and the
generated hosted runtime configuration. The image defines `/ready` as its
built-in health check, so direct Docker consumers retain the same storage and
operational-state readiness contract even when they do not use this repository's
Compose files.

Open:

- Assistant and mobile-web surface: `http://127.0.0.1:8766/`
- Storage management: `http://127.0.0.1:8766/storage`
- Dedicated kiosk display: `http://127.0.0.1:8766/kiosk`

To test from another device on a trusted LAN, opt in for that run:

```sh
MAGPIE_BIND_ADDRESS=0.0.0.0 docker compose up --build -d
```

That exposes an unauthenticated service to the LAN. Prefer the authenticated
hosted configuration for normal phone, tablet, or multi-user access. Do not
forward the local development port to the internet.

The default container stores SQLite data in the named `magpie-data` volume.
Local Compose runs a one-shot initializer before Magpie starts so volumes made
by older root-running images are reassigned to Magpie's unprivileged UID. The
initializer also copies the host-owned runtime configuration into a separate
`magpie-runtime` volume atomically as UID `10001` with mode `0400`. That volume
is mounted read-only by the application and preflight. The application never
needs the source configuration to be world-readable, so a mode-`0600` generated
file works the same way on Linux and Docker Desktop. The application container
itself continues to run without root privileges, with a read-only root
filesystem, no Linux capabilities, and only `/data` plus a bounded temporary
directory writable.
It starts with an empty storage graph, zero configured pixels, and no edge
command queue. Capability discovery therefore reports `lights=false`; no
cabinet commands are created while a new user is setting up rooms, shelves,
totes, or other unlit storage. The physical 128-bin cabinet is only backfilled
by a hardware-enabled configuration.
Production deployments should mount a runtime config that selects Supabase and
provide secrets through the deployment platform. Do not put service-role keys
in an image or browser bundle.

## Hosted Authentication

The default local container keeps `auth.mode=none`. A hosted service should copy
`config/container-supabase.example.json`, replace the Supabase project
placeholder, and start Compose with that file mounted:

```sh
export MAGPIE_CONFIG_FILE=./config/container-hosted.json
export SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
export SUPABASE_SERVICE_ROLE_KEY=...
docker compose up --build -d
```

For a hosted server, use the published multi-architecture image instead of
building from a mutable checkout. Export the runtime values, then run the
non-mutating preparation command:

```sh
export MAGPIE_SUPABASE_URL=https://PROJECT_REF.supabase.co
export MAGPIE_PUBLIC_ORIGIN=https://magpie.example.com
export MAGPIE_IMAGE=ghcr.io/jtalborough/magpie:sha-COMMIT
export MAGPIE_EXISTING_WORKSPACE_ID=EXISTING_WORKSPACE_UUID
export SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
export SUPABASE_SERVICE_ROLE_KEY=...
export OPENAI_API_KEY=...
python3 scripts/prepare-hosted.py
export MAGPIE_CONFIG_FILE=./config/container-hosted.json
docker compose -f compose.hosted.yaml pull
docker compose -f compose.hosted.yaml run --rm magpie-data-init
docker compose -f compose.hosted.yaml --profile verify run --rm magpie-preflight
docker compose -f compose.hosted.yaml up -d
```

`prepare-hosted.py` renders the hardware-neutral, request-scoped Supabase
configuration with mode `0600`, checks the Supabase and agent-provider
credentials, and rejects mutable image tags. It never prints credentials,
pulls an image, starts a container, or changes a running deployment. Use
`OPENROUTER_API_KEY` or the Cloudflare pair
`CLOUDFLARE_AI_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` instead of
`OPENAI_API_KEY` when that provider is selected. Set `MAGPIE_EDGE_ORIGIN` only
when a separately protected machine endpoint is ready.

Set `MAGPIE_EXISTING_WORKSPACE_ID` when the hosted service must attach to an
existing workspace. Preparation then verifies the workspace schema, matching
owner membership, and aggregate inventory visibility before replacing the
generated config. The check is read-only. A failed check preserves any
previously generated config. Omit the variable for a new generic installation
whose users will create workspaces in the visual UI.

For repeatable upgrades, use the guarded host-side promotion command instead
of running the three Compose steps manually:

```sh
export MAGPIE_IMAGE=ghcr.io/jtalborough/magpie:sha-COMMIT
export MAGPIE_CONFIG_FILE=./config/container-hosted.json
export SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
export SUPABASE_SERVICE_ROLE_KEY=...
scripts/deploy-hosted.sh
```

The command refuses mutable image tags, validates that the mounted config is a
hardware-neutral authenticated Supabase service, runs the read-only schema
preflight, promotes the image, and verifies the reported build commit. If the
new container cannot pass its health and central-runtime checks, the command
restores the previously running image. A pinned `@sha256:...` image digest is
also accepted; set `MAGPIE_EXPECTED_COMMIT` when commit verification is desired
for a digest promotion.

The repository also provides a `Deploy hosted Magpie` workflow. A successful
`Publish container` run on `main` triggers it automatically; it can also be
dispatched with a publish run ID and exact commit. It runs on a self-hosted
runner labelled `searchable-storage-central` inside the `magpie-production`
environment, which requires reviewer approval and only accepts `main`.

Because this repository is public, every job that touches a self-hosted runner
or deployment secrets is guarded: it runs only in the owner's repository, never
for `pull_request`/`pull_request_target` events, and manual dispatches must come
from the owner. Fork pull requests also require approval before any workflow
runs. Hostnames, workspace IDs, and credentials live in repository or
environment secrets and variables, never in code.

The preflight exits nonzero unless the service key can query every table needed
by the storage graph, agent history, learned rules, photos, voice evidence, and
workspace membership. It also calls the read-only database capability contract
so an image cannot be promoted before required transactional RPC migrations are
present. In multi-workspace mode it validates the schema without requiring a
workspace to exist yet, so it is safe to run before the first user signs in.
Run it before every hosted upgrade; it does not create or modify data.

Before promoting a hosted image, exercise the complete tenant boundary against
an isolated local Supabase stack:

```sh
python3 scripts/verify_hosted_local.py
```

The harness builds the current checkout, applies the repository migrations,
starts a software-only multi-workspace container on `127.0.0.1:8878`, signs up
two disposable users, and verifies agent-led setup across multiple storage
roots, nested locations, a photo-backed quick put, cross-root move and undo,
learned placement, agent find, QR entry, HTML/PDF labels, archive and restore,
and cross-workspace denial. Product readiness v6 requires the installable
mobile client and its offline capture paths, and retains explicit proof of the
visual multi-root setup, cross-root item move, and archive/restore cycle instead
of inferring management readiness from configured endpoints. The harness
deletes the users and their cascading workspace data when complete. Docker and
Node.js are required; the Supabase CLI is run through `npx`. Pass
`--image IMAGE --skip-build` to verify an already-published immutable image.

Every `Publish container` run executes this journey against the newly published
content digest on an isolated GitHub runner before the workflow can succeed.
The hosted promotion workflow only accepts a verified successful publication
run, making the disposable multi-root journey a release gate rather than an
optional post-deployment check. Successful `main` push publications automatically
promote the central service; central promotion does not deploy the cabinet.
The Pi deployment workflow is manual-only. The gate never connects to production
Supabase.

The hosted definition requires `MAGPIE_IMAGE`; it has no mutable fallback tag.
Use the immutable `sha-*` tag or digest produced by the publish workflow. It
binds only to `127.0.0.1:8766`; terminate HTTPS through Cloudflare or a reverse
proxy on the same host. It also runs as an unprivileged user with a read-only
root filesystem, no Linux capabilities, a persistent `/data` volume, and a
separate read-only `magpie-runtime` volume. Supabase remains authoritative for
inventory and user data; `/data` contains only operational state such as
provider cooldown records and placement-memory notes. A one-shot initializer
repairs data ownership and atomically installs the runtime config in the
runtime volume as UID `10001` with mode `0400` before every start. The guarded
promotion command runs that initializer before both the Supabase preflight and
the new image, and refuses promotion if it fails.

`GET /health` and `GET /ready` include a `magpie.runtime.v1` profile that makes
the process boundary queryable. `GET /ready` also reports an `agent_probe` and
fails closed when a central service has no configured non-deterministic storage
agent. Standalone software-only containers and cabinet services may remain ready
without a cloud model so local storage and hardware workflows keep working. A
production hosted service must report
`role=central_service`, `hardware_owner=false`, `workspace_scope=request`,
`edge_execution=workspace_queue`, and `edge_queue_enabled=true`. A Pi that
owns LEDs reports `role=cabinet_service` and `hardware_owner=true`; the default
software-only local container reports `role=standalone_service`. The guarded
hosted promotion verifies the central profile before accepting a release.

The hosted example is hardware-neutral and multi-workspace: it starts with zero
server-side pixels and `edge.mode=workspace`. After email sign-in, a first-time
user creates a workspace in the app. Existing members can select among their
workspaces from the shared header. Every authenticated request binds its
inventory, locations, rules, photos, interactions, kiosk state, device
credentials, and queued commands to the selected membership.

Unlit storage does not require an edge device. To connect a cabinet or another
light controller, pair a commands-capable device in `/account`, run the
expiring setup command on the controller, then bind a storage location to that
device ID and its device-local pixel indexes in the Storage view. The permanent
credential is delivered directly to the controller and is never shown in the
browser. Magpie routes each light action through the location's `light_target`;
different workspaces and controllers may reuse the same pixel indexes without
sharing state.

The Cabinet setup command deliberately activates only the edge light worker.
It leaves the existing local voice listener and kiosk unchanged. After the
central inventory import and edge-light route are verified, rerun the connector
without the expiring pairing code and add `--activate-central-experience` to
move those two user experiences to the central workspace.

`config/container-supabase-edge.example.json` remains available for a legacy
single-workspace central service with one fixed target. The optional
`MAGPIE_EDGE_TOKENS_JSON` value remains available only as a migration fallback:

```sh
cp config/container-supabase-edge.example.json config/container-hosted.json
export MAGPIE_EDGE_TOKENS_JSON='{"main-cabinet":"a-long-random-device-token"}'
```

The enrolled token also provides the narrowly scoped voice bridge needed for a
central cutover. Set the Pi voice service's `MAGPIE_VOICE_API_URL` to the
protected HTTPS origin and keep `MAGPIE_EDGE_DEVICE_ID` plus
`MAGPIE_EDGE_TOKEN` in its existing `secrets.env`. Voice chat,
confirm/cancel/undo, live state, and acceptance evidence then use the same
central workspace as web and mobile. All other protected APIs continue to
require a human Supabase session. Leave `MAGPIE_VOICE_API_URL` unset until the
central import and edge queue have passed the migration gate.

Device pairing uses least-privilege role presets. Choose `Cabinet` for the
combined Pi controller, kiosk, and voice daemon; choose the narrower role for a
standalone controller, screen, voice station, printer, or check station.
Reported heartbeat capabilities are diagnostic only and cannot add access.
Replacement pairing keeps the assigned scopes and the current credential stays
active until the controller completes the exchange. Existing managed
credentials migrate to command-only access and must be re-enrolled with the
appropriate role before a hosted voice or kiosk cutover. The static
`MAGPIE_EDGE_TOKENS_JSON` fallback
remains broadly authorized for compatibility and should be removed after that
cutover.

Do not put the customer app origin behind Cloudflare Access. The browser must
reach `/auth/config` directly so Magpie can present Supabase sign-in, and the
mobile PWA and QR links must work without a separate operator login. Supabase
sessions and workspace membership protect human storage data.
The hosted API also limits browser CORS access to `public_origin`; unrelated
sites cannot use a browser to call the API. The local development configuration
keeps permissive CORS for standalone LAN tools and remains loopback-bound by
default.

For defense in depth, route a second machine-only hostname to the same central
container and protect that hostname with Cloudflare Access Service Auth. Set
`edge.machine_url` (or the hosted renderer's `--edge-origin`) to that hostname;
device pairing commands will use it while browser links continue to use
`public_origin`. Store the service-token pair as
`MAGPIE_CF_ACCESS_CLIENT_ID` and `MAGPIE_CF_ACCESS_CLIENT_SECRET` in the same
mode-`0600` Pi environment file. These headers cross Cloudflare; the independent
Magpie edge token is still required for application authorization.
For the `magpie-production` GitHub environment, store the same pair as
`MAGPIE_CF_ACCESS_CLIENT_ID` and `MAGPIE_CF_ACCESS_CLIENT_SECRET`, then set the
`MAGPIE_EDGE_ORIGIN` environment variable. These settings are optional for a
software-only release. Once `MAGPIE_EDGE_ORIGIN` is configured, hosted promotion
requires both credentials and fails unless the authenticated machine origin
returns the expected Magpie build and PWA.

Validate each boundary explicitly:

```sh
MAGPIE_ORIGIN_MODE=public \
  MAGPIE_PUBLIC_ORIGIN=https://magpie.example.com \
  scripts/check-secure-origin.sh

MAGPIE_ORIGIN_MODE=access \
  MAGPIE_PUBLIC_ORIGIN=https://edge.magpie.example.com \
  CF_ACCESS_CLIENT_ID=... \
  CF_ACCESS_CLIENT_SECRET=... \
  scripts/check-secure-origin.sh
```

GitHub publishes `linux/amd64` and `linux/arm64` images on every `main` update
and version tag. The repository owner must make the GHCR package public once if
anonymous servers should pull it; otherwise log the host in with a package-read
token before `docker compose pull`.

The publishable key is intentionally returned by `/auth/config` for browser
login. The service-role key stays in the container and is used only to verify
workspace membership and operate the server repositories. Every protected API
request validates the Supabase user session and requires a row in
`workspace_members` for the configured workspace. Device polling uses the
separate edge token and never accepts a human session token.

Before enabling hosted sign-in:

1. Add the public Magpie origin to Supabase Auth redirect URLs.
2. Configure custom SMTP; Supabase's trial sender is restricted and is not a
   production delivery service.
3. Enable the intended Supabase Auth providers. New authenticated users create
   their first workspace in Magpie; workspace owners add members from the
   account surface. For a fixed hardware workspace, provision membership with
   the administrative script instead.
4. Keep Cloudflare, a reverse proxy, or the hosting platform on HTTPS. Keep the
   customer hostname public at the network boundary and use the optional
   Access-protected machine hostname for edge clients. The edge worker refuses
   a non-HTTPS central URL outside localhost.
5. Verify sign-in, refresh, sign-out, denied membership, and API access before
   exposing the origin.

To run only the read-only attachment diagnostic without preparing a hosted
configuration:

```sh
export SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
searchable-storage-supabase-check \
  --config config/container-supabase.example.json \
  --verify-attachment \
  --workspace-id 00000000-0000-4000-8000-000000000000
```

The attachment check verifies the complete schema, confirms that the workspace
owner has a matching `owner` membership, and prints aggregate membership and
inventory counts. It does not write to Supabase or print user IDs, emails, or
credentials. A passing check means the central service can select the existing
workspace after sign-in; it does not redirect the Pi or deploy anything.

### Provision A Fixed Hardware Workspace

The generic hosted service creates workspaces in the app. Use the provisioning
command when a deployment must be pinned to one hardware-owning workspace, or
when migrating the existing cabinet without hand-editing database rows:

```sh
export SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
searchable-storage-provision-workspace \
  --supabase-url https://PROJECT_REF.supabase.co \
  --workspace-name "My Home" \
  --invite-email owner@example.com \
  --redirect-url https://magpie.example.com/
```

The command prints the generated workspace ID plus matching `storage` and
`auth` configuration blocks. Workspace creation is idempotent for the same
`--owner-user-id` and workspace name. An email invitation is a one-time setup
action; after the user exists, use their Supabase user UUID with
`--owner-user-id` instead of repeating `--invite-email`.

This command requires the server-side Supabase secret key because invitations
are an Auth Admin operation. Never run it in a browser or expose the key in the
generated configuration.

In a fixed deployment, use the generated `storage` and `auth` configuration
blocks to replace the matching blocks in the edge example. After signing in,
the owner can open `/account` to invite people, assign roles,
remove access, and pair edge devices. Owners and admins can create replacement
pairings or revoke device credentials; permanent credentials are delivered only
to the target device. Owners can manage every non-owner membership. Admins can
manage members and viewers but cannot create, demote, or remove another admin.
Members can operate storage, while viewers have read-only API access.

## Migration Gate

Do not redirect the cabinet kiosk or stop the Pi API until the container passes:

1. API and agent regression tests.
2. Import of a production export into an isolated workspace.
3. Find, put-away, confirm, correction, photo, and label journeys.
4. Kiosk and mobile viewport checks.
5. Human and edge authentication, retry, and offline behavior tests.

Use `GET /export/inventory.json` for a portable, photo-inclusive backup before
any migration attempt. Validate it against the isolated target first with
`POST /import/inventory.json` and `{"dry_run": true}`. The v1 snapshot restores
the complete active and archived location graph, every exported item placement,
item dimensions and size provenance, home and last-seen locations, review state
and reason, item lifecycle state, referenced item and location photos, and both
active and disabled placement and size rules. Existing minimal v1 snapshots
remain accepted; fields absent from older snapshots keep their normal import
defaults.

When referenced photos exceed the portable JSON endpoint's 18 MB limit, create
a ZIP bundle instead. The bundle keeps graph and rule metadata in
`manifest.json`, stores every referenced photo as an individual file, records a
SHA-256 digest and byte count for each photo, and validates the complete archive
before reporting success:

```sh
searchable-storage-backup create \
  --url http://127.0.0.1:8765 \
  --output "$HOME/Documents/Magpie Backups/main-cabinet.zip"

searchable-storage-backup verify \
  "$HOME/Documents/Magpie Backups/main-cabinet.zip"
```

For an authenticated hosted workspace, put the complete bearer value in an
environment variable rather than a command-line argument, then add the
workspace selector:

```sh
export MAGPIE_BACKUP_AUTHORIZATION="Bearer ..."
searchable-storage-backup create \
  --url https://magpie.example.com \
  --authorization-env MAGPIE_BACKUP_AUTHORIZATION \
  --workspace-id WORKSPACE_UUID \
  --output "$HOME/Documents/Magpie Backups/workspace.zip"
```

The command does not mutate the source service. Backup files and temporary
files are mode `0600`; verification checks the storage graph, rule
destinations, photo references, byte counts, and checksums without displaying
item names or media.

The direct `searchable-storage-migrate-supabase` command remains the preferred
cabinet-to-hosted path when the original SQLite database and photo files are
available because it additionally preserves agent examples, interaction and
voice history, hardware bindings, and repository events. The portable JSON
backup is the operator-readable recovery artifact and the cross-install import
path; keep both until the central workspace has passed the migration gate.

The Supabase portion of the gate is executable and read-only:

```sh
docker compose -f compose.hosted.yaml --profile verify run --rm magpie-preflight
```
