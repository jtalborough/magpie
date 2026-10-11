-- Durable command handoff from the central Magpie service to trusted edge devices.

create table if not exists public.edge_devices (
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  device_id text not null,
  version text not null default '',
  capabilities text[] not null default '{}',
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, device_id)
);

create table if not exists public.edge_commands (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  device_id text not null,
  kind text not null check (kind in ('set_pixels')),
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'queued'
    check (status in ('queued', 'claimed', 'succeeded', 'failed')),
  attempts integer not null default 0 check (attempts >= 0),
  created_at timestamptz not null default now(),
  not_before timestamptz not null default now(),
  expires_at timestamptz not null,
  claimed_at timestamptz,
  completed_at timestamptz,
  error text not null default ''
);

create index if not exists edge_commands_claim_idx
  on public.edge_commands(workspace_id, device_id, status, not_before, created_at);

drop trigger if exists edge_devices_set_updated_at on public.edge_devices;
create trigger edge_devices_set_updated_at
before update on public.edge_devices
for each row execute function public.set_updated_at();

alter table public.edge_devices enable row level security;
alter table public.edge_commands enable row level security;

drop policy if exists "workspace members can use edge devices" on public.edge_devices;
create policy "workspace members can use edge devices"
on public.edge_devices for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

drop policy if exists "workspace members can use edge commands" on public.edge_commands;
create policy "workspace members can use edge commands"
on public.edge_commands for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

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
  if command_kind <> 'set_pixels' then
    raise exception 'unsupported edge command kind';
  end if;
  if ttl_seconds <= 0 then
    raise exception 'edge command ttl must be positive';
  end if;
  update public.edge_commands
  set status = 'failed', completed_at = now(), error = 'superseded by newer state'
  where workspace_id = target_workspace_id
    and device_id = target_device_id
    and kind = command_kind
    and status = 'queued';
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

create or replace function public.magpie_claim_edge_command(
  target_workspace_id uuid,
  target_device_id text,
  claim_timeout_seconds double precision default 30,
  attempt_limit integer default 5
) returns jsonb
language plpgsql
as $$
declare
  selected_id uuid;
  claimed public.edge_commands;
begin
  update public.edge_commands
  set status = 'queued', claimed_at = null
  where workspace_id = target_workspace_id
    and device_id = target_device_id
    and status = 'claimed'
    and claimed_at < now() - make_interval(secs => claim_timeout_seconds)
    and attempts < attempt_limit
    and expires_at > now();

  update public.edge_commands
  set status = 'failed', completed_at = now(), error = 'expired or retry limit reached'
  where workspace_id = target_workspace_id
    and device_id = target_device_id
    and status in ('queued', 'claimed')
    and (expires_at <= now() or attempts >= attempt_limit);

  select id into selected_id
  from public.edge_commands
  where workspace_id = target_workspace_id
    and device_id = target_device_id
    and status = 'queued'
    and not_before <= now()
    and expires_at > now()
  order by created_at
  for update skip locked
  limit 1;

  if selected_id is null then
    return null;
  end if;

  update public.edge_commands
  set status = 'claimed', claimed_at = now(), attempts = attempts + 1
  where id = selected_id
  returning * into claimed;
  return to_jsonb(claimed);
end;
$$;

drop function if exists public.magpie_complete_edge_command(uuid, text, uuid, boolean, text);
create or replace function public.magpie_complete_edge_command(
  target_workspace_id uuid,
  target_device_id text,
  target_command_id uuid,
  command_succeeded boolean,
  command_error text default '',
  command_retryable boolean default false,
  attempt_limit integer default 5
) returns jsonb
language plpgsql
as $$
declare
  completed public.edge_commands;
begin
  update public.edge_commands
  set status = case
        when command_succeeded then 'succeeded'
        when command_retryable and attempts < attempt_limit and expires_at > now() then 'queued'
        else 'failed'
      end,
      completed_at = case
        when not command_succeeded and command_retryable
          and attempts < attempt_limit and expires_at > now() then null
        else now()
      end,
      claimed_at = case
        when not command_succeeded and command_retryable
          and attempts < attempt_limit and expires_at > now() then null
        else claimed_at
      end,
      not_before = case
        when not command_succeeded and command_retryable
          and attempts < attempt_limit and expires_at > now()
          then now() + make_interval(secs => least(30, power(2, attempts))::double precision)
        else not_before
      end,
      error = left(coalesce(command_error, ''), 1000)
  where id = target_command_id
    and workspace_id = target_workspace_id
    and device_id = target_device_id
    and status = 'claimed'
  returning * into completed;
  if completed.id is null then
    return null;
  end if;
  return to_jsonb(completed);
end;
$$;
