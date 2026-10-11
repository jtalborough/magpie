create or replace function public.magpie_enqueue_edge_command(
  target_workspace_id uuid,
  target_device_id text,
  command_kind text,
  command_payload jsonb,
  ttl_seconds double precision default 300
) returns jsonb
language plpgsql
as $$
declare
  queued public.edge_commands;
begin
  if command_kind not in ('set_pixels', 'print_zpl') then
    raise exception 'unsupported edge command kind';
  end if;
  if ttl_seconds <= 0 then
    raise exception 'edge command ttl must be positive';
  end if;
  if command_kind = 'set_pixels' then
    update public.edge_commands
    set status = 'failed', completed_at = now(), error = 'superseded by newer state'
    where workspace_id = target_workspace_id
      and device_id = target_device_id
      and kind = command_kind
      and status = 'queued';
  end if;
  insert into public.edge_commands(
    workspace_id, device_id, kind, payload, expires_at
  ) values (
    target_workspace_id, target_device_id, command_kind,
    coalesce(command_payload, '{}'::jsonb),
    now() + make_interval(secs => ttl_seconds)
  ) returning * into queued;
  return to_jsonb(queued);
end;
$$;
