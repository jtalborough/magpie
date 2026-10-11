-- Workspace-managed edge credentials. Tokens are generated with high entropy by
-- Magpie and only their SHA-256 digests are persisted.

alter table public.edge_devices
  add column if not exists credential_hash text not null default '',
  add column if not exists credential_hint text not null default '',
  add column if not exists credential_created_at timestamptz,
  add column if not exists revoked_at timestamptz;

comment on column public.edge_devices.credential_hash is
  'SHA-256 digest of the device bearer token; plaintext is returned only at issuance.';

-- Edge devices are managed only through Magpie's authenticated server API.
-- A column-level revoke would not override a broader table-level grant.
revoke all privileges on table public.edge_devices from anon, authenticated;
