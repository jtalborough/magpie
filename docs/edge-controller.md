# Magpie edge controller

The central Magpie service owns inventory, the storage graph, agent decisions,
and durable command state. A cabinet Pi remains responsible for LAN hardware and
executes a small allowlist of commands. It does not receive a Supabase key.

## Trust boundary

- Each controller has a stable `device_id` and a separate random bearer token.
- Workspace owners and admins pair, rotate, and revoke managed devices from
  `/account`. The default pairing flow shows the browser only an expiring,
  one-use setup code. The device exchanges it over HTTPS, receives the
  permanent credential directly, and stores it in a mode-`0600` secrets file.
- Pairing and credential secrets are stored only as SHA-256 digests. Pairing
  codes expire after ten minutes and are consumed atomically. Manual token
  enrollment remains available through the API for migration compatibility.
- Enrollment uses a device role preset to grant explicit scopes. Cabinet devices
  receive `commands`, `screen`, and `voice`; single-purpose controllers,
  screens, voice stations, and printers receive only their matching scope.
  Rotation preserves the existing grants. The `check_station` scope is reserved
  for the later dedicated station bridge; the current check-station web tool uses
  normal workspace authentication and is not offered as an edge-device profile.
- Heartbeat capabilities are self-reported operational metadata. They never
  grant access or expand a credential's server-assigned scopes.
- The central service also reads `MAGPIE_EDGE_TOKENS_JSON` as a migration
  fallback. Those static credentials retain broad legacy access, so remove a
  device from that map after its managed scoped token is working.
- The Pi reads `MAGPIE_CENTRAL_URL`, `MAGPIE_EDGE_DEVICE_ID`, and
  `MAGPIE_EDGE_TOKEN` from its local `secrets.env`. When the central origin is
  protected by Cloudflare Access, the same protected file also contains
  `MAGPIE_CF_ACCESS_CLIENT_ID` and `MAGPIE_CF_ACCESS_CLIENT_SECRET`.
- The Cloudflare service token authenticates the Pi to the network boundary;
  the separate Magpie device token authorizes the device and its scopes inside
  the application. Neither credential substitutes for the other.
- For a customer-facing deployment, keep the human app hostname public and use
  a separate Access-protected machine hostname. Set `edge.machine_url` on the
  central service so generated pairing commands use the machine boundary
  without placing browser sign-in behind Cloudflare Access.
- Multi-workspace deployments fail closed when that machine URL is absent:
  Account reports that pairing is unavailable and the pairing API returns
  `503` instead of issuing a setup code that cannot reach the central service.
- Token digests are compared in constant time. Plaintext tokens are never
  written to the database or returned after enrollment or rotation.
- The protocol permits complete `set_pixels` states and validated `print_zpl`
  jobs. The worker rejects unknown commands before contacting the local
  controller.

Example fallback central secret value:

```json
{"main-cabinet":"generate-a-long-random-token"}
```

## Delivery behavior

Commands move through `queued`, `claimed`, and `succeeded` or `failed` states.
Claims are atomic in Supabase, abandoned claims become eligible for retry, and
transient execution failures use bounded backoff. Expired, malformed, or
over-limit commands fail closed. A newer unclaimed pixel state
supersedes an older one so a cabinet does not replay stale lighting.

Idle workers keep one command claim open for up to 20 seconds instead of
polling every second. Device heartbeats are sent about once per minute, and
connection failures use bounded exponential backoff. The defaults reduce an
idle controller from roughly 172,800 requests per day to about 5,800 while
still waking immediately when this service instance enqueues a light change.
`MAGPIE_EDGE_WAIT_SECONDS`, `MAGPIE_EDGE_HEARTBEAT_SECONDS`, and
`MAGPIE_EDGE_POLL_SECONDS` can tune those intervals without changing the unit.

The operator endpoint `GET /edge/status` reports device heartbeats, queue counts,
and recent command outcomes. Device endpoints require the matching bearer token.
Authenticated workspace members can list devices at `GET /workspace/devices`;
only owners and admins can create or replace pairings, use the compatibility
token endpoints, or revoke credentials.

Each device returned by `GET /workspace/devices` includes a derived connection
state. `connected` requires a heartbeat within the last three minutes, while
`command_ready` additionally requires the server-granted `commands` scope and
the device-reported `set_pixels` capability. `connection_state`,
`connection_message`, and `last_seen_age_seconds` let account clients distinguish
an unpaired device, a pending setup, a paired device waiting for its first
heartbeat, a connected device, and a stale/offline device without interpreting
timestamps themselves. The account screen watches for this first heartbeat
after pairing and reports when a cabinet is actually ready for light commands.

A cabinet credential can authenticate both its controller and voice daemon to
the hosted Magpie agent because its role grants `commands`, `screen`, and
`voice`. A voice-only credential is accepted only for assistant chat, spoken
confirm/cancel/undo, and voice state/evidence reporting. A screen credential is
accepted only for kiosk state and heartbeat access. None is a general inventory
or administration token. Configure these values when the central service
becomes authoritative:

```bash
MAGPIE_API_URL=https://magpie-edge.example.com
MAGPIE_EDGE_DEVICE_ID=main-cabinet
MAGPIE_EDGE_TOKEN=the-one-time-enrollment-token
MAGPIE_CF_ACCESS_CLIENT_ID=cloudflare-service-token-id.access
MAGPIE_CF_ACCESS_CLIENT_SECRET=cloudflare-service-token-secret
```

The dedicated kiosk stays on a loopback URL even after a central cutover. The
Pi screen bridge injects the cabinet credential server-side and forwards only
build metadata, the combined kiosk state channel, and screen heartbeats. The
token is never placed in Chromium storage, a query string, or the page source:

```bash
MAGPIE_SCREEN_API_URL=https://magpie-edge.example.com \
  scripts/install-pi5-kiosk.sh
```

Without `MAGPIE_SCREEN_API_URL`, the installer opens the local Pi service at
`http://127.0.0.1:8765/kiosk`. A remote screen origin must use HTTPS, and the
bridge itself refuses to bind outside loopback.

Central experience activation is transactional. The cabinet connector snapshots
the existing voice, screen-bridge, and kiosk user-service definitions plus their
active and enabled states before redirecting them. If either installer fails, a
service does not start, or the secure screen bridge never becomes healthy, the
connector restores and verifies the previous local voice and kiosk state. The
edge light worker remains connected to central Magpie so inventory and light
routing can continue to be verified before another activation attempt.

The voice daemon refuses to send an edge token to remote plain HTTP. Its local
default remains `http://127.0.0.1:8765`, so installing this capability does not
redirect the working cabinet by itself.

## Pi activation

Keep the existing `searchable-storage-led` service on `127.0.0.1:8765`. In the
hosted account, choose a device ID and role, select **Pair device**, and run the
generated setup command on the target device. A Cabinet pairing uses
`scripts/connect-pi5-cabinet.sh` to exchange the temporary code without
printing the permanent credential, then connects the light worker to the
central workspace while leaving the working local voice listener and kiosk
unchanged. The local Pi API remains active as the loopback hardware controller
and fallback POC. After the central inventory has been imported and verified,
rerun the connector without the pairing code and add
`--activate-central-experience` to move the voice listener and secure kiosk
bridge to that same central workspace.

Protect only the machine hostname with Cloudflare Access. First create an
Access service token and add a Service Auth policy that accepts it for the
Magpie edge application.
Provision it on the Pi without placing it in shell history:

```bash
MAGPIE_CF_ACCESS_CLIENT_ID=... \
MAGPIE_CF_ACCESS_CLIENT_SECRET=... \
  scripts/configure-pi-edge-access.sh
```

The deployment workflow's **Configure edge machine access** option performs the
same operation from the repository's `MAGPIE_CF_ACCESS_CLIENT_ID` and
`MAGPIE_CF_ACCESS_CLIENT_SECRET` secrets. The edge worker, voice daemon, and
kiosk screen bridge then attach the Cloudflare headers only to central requests.
The one-shot pairing CLI reads only those two values from `secrets.env`; it does
not source or export the file's other application secrets.

For an already configured credential, install the worker directly:

```bash
MAGPIE_CENTRAL_URL=https://magpie-edge.example.com \
  scripts/install-pi5-edge-service.sh
```

Do not activate the worker against the cabinet's own public tunnel URL. The
central URL must resolve to the separately hosted service. The shared hosted
service uses `edge.mode=workspace` and routes by each location's
`light_target`; a legacy single-workspace service may use `edge.mode=central`
with one fixed target.
