# Magpie MCP connections

Hosted Magpie exposes its guarded storage tools at `/mcp/`. Workspace owners
and admins create a connection from **Account > Agent connections**.

Each connection receives a token beginning with `magpie_mcp_`. The plaintext
token is shown once. Magpie stores only its SHA-256 digest and a six-character
hint. A token is bound to one workspace, works only on the MCP route, and can be
revoked without changing a person's login or an edge device credential.

Configure an MCP client with:

```text
URL: https://magpie.example/mcp/
Authorization: Bearer magpie_mcp_...
```

The MCP tool surface allows reads, lighting, and confirmation-gated storage
proposals. Inventory and storage-graph writes remain pending until confirmed by
the normal Magpie interaction flow.

## Cloudflare boundary

An Access-protected deployment must allow MCP requests to reach Magpie's own
Bearer-token middleware. Keep the general web application behind its existing
human access policy and expose only `/mcp/*` through an application or policy
that delegates authentication to the workspace-scoped Magpie token. Do not
reuse a Cloudflare API token, Supabase key, human session, or edge-device token.
