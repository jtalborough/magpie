# Agent Eval Replay

Confirmed commands are the lightweight regression set for Magpie's storage
agent. Use replay before changing prompts, models, placement scoring, or voice
routing.

Start with a bounded dry run to see which approved examples are replay-ready:

```bash
searchable-storage-evals --limit 25
```

When intentionally testing the live model, run:

```bash
searchable-storage-evals --limit 25 --run-model
```

The command exits nonzero if any replayed example fails or errors, or when
fewer than the required number of cases are replay-ready. A zero-case or
all-skipped run is therefore not a successful release check. Set
`MAGPIE_API_URL` or pass `--base-url` to point at a different running Magpie
API.

Replay uses only examples that have been explicitly approved or promoted in
Magpie's learning review. Pending examples are excluded. Require a larger
evidence floor before accepting a prompt or model change:

```bash
searchable-storage-evals --limit 25 --min-cases 10 --run-model
```

`--min-cases` is capped at 25, matching the server-side live-model ceiling.
Skipped historical examples do not count toward the floor.

Case count alone does not prove workflow diversity. A release check can require
one or more reviewed scenarios:

```bash
searchable-storage-evals \
  --limit 25 \
  --min-cases 10 \
  --require-scenario putaway \
  --require-scenario rules \
  --require-scenario correction \
  --require-scenario photo \
  --run-model
```

Available scenarios are `putaway`, `rules`, `correction`, `photo`, and
`organization`. A skipped case does not satisfy its scenario requirement. This
keeps a large set of rule examples from standing in for put-away or photo
coverage.

For the hosted multi-workspace service, supply the signed-in user's short-lived
Supabase access token through the environment rather than a command-line
argument. Select the workspace explicitly so the replay cannot drift into a
different membership:

```bash
MAGPIE_API_URL=https://magpie.example.com \
MAGPIE_AUTH_TOKEN=the-current-user-access-token \
MAGPIE_WORKSPACE_ID=the-workspace-id \
  searchable-storage-evals --limit 25 --run-model
```

If the operator endpoint is behind a machine-only Cloudflare Access boundary,
also set `MAGPIE_CF_ACCESS_CLIENT_ID` and
`MAGPIE_CF_ACCESS_CLIENT_SECRET`. Both values are required together. The CLI
sends the Cloudflare service headers at the network boundary and the user JWT
plus `X-Magpie-Workspace` at the application boundary. It never prints any of
these credentials.

Live-model replay is restricted to workspace owners and admins and is capped at
25 examples per request even when a larger `--limit` is supplied. Dry replay
does not call the model and may inspect up to the general 500-example API cap.
The response reports `requested_limit`, `effective_limit`, and `limit_capped`
so automation can verify the spend boundary it actually received.

Useful defaults:

- `--kind confirmed`: optionally limit replay to confirmed examples; the
  default includes all reviewed kinds.
- `--limit 25`: keep live-model runs bounded.
- `--min-cases 1`: fail closed unless at least one case is replay-ready or
  evaluated; raise this for release gates.
- `--require-scenario`: require reviewed evidence for a workflow; repeat the
  flag for multiple scenarios.
- `--workspace-id`: override `MAGPIE_WORKSPACE_ID` without placing a secret in
  the process list.
- `--json`: emit the raw `/assistant/evals/replay` response for CI logs or
  deeper inspection.
