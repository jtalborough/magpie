-- Server-assigned least-privilege scopes for managed edge credentials.
-- Heartbeat capabilities remain device-reported operational metadata and do
-- not grant access.

alter table public.edge_devices
  add column if not exists credential_scopes text[] not null
    default array['commands']::text[];

update public.edge_devices
set credential_scopes = array['commands']::text[]
where credential_scopes is null or cardinality(credential_scopes) = 0;

alter table public.edge_devices
  drop constraint if exists edge_devices_credential_scopes_valid;

alter table public.edge_devices
  add constraint edge_devices_credential_scopes_valid check (
    cardinality(credential_scopes) > 0
    and credential_scopes <@ array[
      'commands', 'voice', 'screen', 'printer', 'check_station'
    ]::text[]
  );

comment on column public.edge_devices.credential_scopes is
  'Server-assigned permissions for this credential; never inferred from heartbeat capabilities.';
