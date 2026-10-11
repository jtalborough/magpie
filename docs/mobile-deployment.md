# Mobile deployment

Magpie ships as a progressive web app for iPhone, iPad, and Android. A real
phone installation requires the central service on a secure HTTPS origin. The
Pi's local HTTP URL remains available only to the cabinet kiosk, voice daemon,
and hardware controller.

## Recommended ingress

Route two hostnames through a remotely managed Cloudflare Tunnel running on the
central container host:

1. Route `magpie.example.com` to the central service, currently
   `http://127.0.0.1:8766` on the hosted runner.
2. Leave that customer hostname public at the Cloudflare network boundary.
   Supabase sessions and workspace membership protect inventory APIs.
3. Route `magpie-edge.example.com` to the same central service.
4. Protect only the edge hostname with a Cloudflare Access Service Auth policy.
5. Set the hosted `MAGPIE_PUBLIC_ORIGIN` and `MAGPIE_EDGE_ORIGIN` environment
   variables to those HTTPS origins.
6. Pair each Pi as an outbound edge controller. The device uses both its scoped
   Magpie credential and the Cloudflare service token for the edge hostname.

Plan the Cloudflare changes first. This command is read-only:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-central-cloudflare.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com
```

Provisioning is deliberately split into two apply phases. `prepare` creates the
dedicated remotely managed tunnel, configures ingress, and writes the connector
token to a mode-0600 file. It does not alter DNS or Access:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-central-cloudflare.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com \
    --apply prepare \
    --token-file "$HOME/.config/magpie-central/cloudflared-token"
```

Then install its connector on the central host:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=...
MAGPIE_LOCAL_ORIGIN=http://127.0.0.1:8766 \
  scripts/install-central-cloudflare-tunnel.sh
```

The installer refuses to start when the local central service is unhealthy or
when the upstream is not a loopback HTTP origin. It does not create or alter DNS,
ingress routes, Access applications, policies, or service tokens.

Plan the Access boundary independently. This is also read-only and reports the
exact application IDs that a later public release would remove:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-cloudflare-access.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com
```

Prepare the machine-only boundary before changing the customer hostname. This
creates one self-hosted edge application, a named service token, and a Service
Auth policy bound specifically to that token. The client ID and one-time secret
are written to a mode-0600 environment file and are never printed:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-cloudflare-access.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com \
    --apply prepare \
    --credential-file "$HOME/.config/magpie-edge/cloudflare-access.env"
```

Only after the central connector is healthy, switch DNS. The expected current
tunnel ID is a compare-and-swap guard against replacing an origin that changed
since it was inspected:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-central-cloudflare.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com \
    --apply cutover \
    --expected-current-public-tunnel-id CURRENT_PI_TUNNEL_ID
```

After the edge hostname and service token pass verification, remove Access from
the customer hostname. Repeat `--expected-public-app-id` for every ID shown by
the plan. The command refuses the change if those IDs have changed or the edge
Service Auth policy is not bound to the existing token:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/provision-cloudflare-access.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com \
    --apply release-public \
    --expected-public-app-id CURRENT_PUBLIC_ACCESS_APP_ID
```

Do not route the customer hostname to the Pi or put it behind Cloudflare
Access. Both prevent normal Supabase sign-in, mobile installation, and public
QR entry before Magpie can apply its own authorization.

The optional `install_secure_tunnel` Pi workflow input is retained only for a
legacy, owner-only POC hostname. It is not the production customer ingress and
must not own `magpie.example.com`.

## Verification

After DNS is active:

```bash
CLOUDFLARE_API_TOKEN=... \
  scripts/audit-cloudflare-boundaries.py \
    --zone example.com \
    --public-host magpie.example.com \
    --edge-host magpie-edge.example.com \
    --expected-service http://127.0.0.1:8766

MAGPIE_ORIGIN_MODE=public \
  MAGPIE_PUBLIC_ORIGIN=https://magpie.example.com \
  scripts/check-secure-origin.sh

MAGPIE_ORIGIN_MODE=access \
  MAGPIE_PUBLIC_ORIGIN=https://magpie-edge.example.com \
  CF_ACCESS_CLIENT_ID=... \
  CF_ACCESS_CLIENT_SECRET=... \
  scripts/check-secure-origin.sh
```

On iPhone, open the HTTPS URL in Safari and use **Add to Home Screen**. On
Android, open it in Chrome and choose **Install app**. Verify text search,
camera capture, photo upload, and a put-away confirmation from each device.

Unauthenticated browser requests may load the application shell and auth
configuration, but inventory and physical actions still require a valid
Supabase session and workspace membership. Edge requests additionally require
a scoped device token; the machine hostname's Access policy is defense in
depth, not a substitute for application authorization.
