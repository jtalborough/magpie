-- Durable credentials let non-browser agents connect to one workspace over MCP.
-- Plaintext tokens are shown once and never stored.

create table public.agent_credentials (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  token_hash text not null unique check (char_length(token_hash) = 64),
  token_hint text not null check (char_length(token_hint) = 6),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);

create index agent_credentials_workspace_created_idx
  on public.agent_credentials(workspace_id, created_at desc);

alter table public.agent_credentials enable row level security;

revoke all on table public.agent_credentials from anon, authenticated;
grant select, insert, update, delete on table public.agent_credentials to service_role;

comment on table public.agent_credentials is
  'Revocable workspace-scoped credentials accepted only by the Magpie MCP endpoint.';
comment on column public.agent_credentials.token_hash is
  'SHA-256 digest of a magpie_mcp_ token; plaintext is returned once and never stored.';
