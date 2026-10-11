-- Repair projects whose original migration baseline recorded edge scopes
-- before the column and pairing function existed in the live schema.

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

create or replace function public.magpie_consume_edge_pairing(
  target_workspace_id uuid,
  target_device_id text,
  presented_pairing_hash text,
  new_credential_hash text,
  new_credential_hint text
) returns jsonb
language plpgsql
as $$
declare
  paired public.edge_devices;
begin
  update public.edge_devices
  set credential_hash = new_credential_hash,
      credential_hint = new_credential_hint,
      credential_scopes = pairing_scopes,
      credential_created_at = now(),
      revoked_at = null,
      pairing_hash = '',
      pairing_hint = '',
      pairing_scopes = '{}',
      pairing_expires_at = null
  where workspace_id = target_workspace_id
    and device_id = target_device_id
    and pairing_hash <> ''
    and pairing_hash = presented_pairing_hash
    and pairing_expires_at > now()
  returning * into paired;

  if paired.device_id is null then
    return null;
  end if;

  return jsonb_build_object(
    'device_id', paired.device_id,
    'credential_hint', paired.credential_hint,
    'credential_scopes', paired.credential_scopes,
    'credential_created_at', paired.credential_created_at,
    'revoked_at', paired.revoked_at
  );
end;
$$;

revoke all on function public.magpie_consume_edge_pairing(
  uuid, text, text, text, text
) from public, anon, authenticated;
grant execute on function public.magpie_consume_edge_pairing(
  uuid, text, text, text, text
) to service_role;

notify pgrst, 'reload schema';
