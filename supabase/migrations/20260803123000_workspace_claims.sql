create table if not exists public.workspace_claims (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  email text not null,
  role text not null default 'owner'
    check (role in ('owner', 'admin', 'member', 'viewer')),
  transfer_ownership boolean not null default false,
  status text not null default 'pending'
    check (status in ('pending', 'claimed', 'revoked')),
  expires_at timestamptz,
  claimed_by uuid references auth.users(id) on delete set null,
  claimed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists workspace_claims_pending_email_idx
  on public.workspace_claims(workspace_id, lower(email))
  where status = 'pending';

alter table public.workspace_claims enable row level security;
revoke all privileges on table public.workspace_claims from anon, authenticated;
grant select, insert, update on table public.workspace_claims to service_role;

create or replace function public.magpie_pending_workspace_claims(
  target_user_id uuid,
  target_email text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_email text := lower(trim(coalesce(target_email, '')));
begin
  if not exists (
    select 1
    from auth.users u
    where u.id = target_user_id
      and lower(coalesce(u.email, '')) = normalized_email
  ) then
    return '[]'::jsonb;
  end if;

  return coalesce((
    select jsonb_agg(
      jsonb_build_object(
        'id', c.id,
        'workspace_id', c.workspace_id,
        'workspace_name', w.name,
        'role', c.role,
        'transfer_ownership', c.transfer_ownership,
        'expires_at', c.expires_at,
        'location_count', (
          select count(*) from public.locations l
          where l.workspace_id = c.workspace_id and l.active
        ),
        'inventory_count', (
          select count(*) from public.items i
          where i.workspace_id = c.workspace_id and i.active
        )
      )
      order by c.created_at
    )
    from public.workspace_claims c
    join public.workspaces w on w.id = c.workspace_id
    where lower(c.email) = normalized_email
      and c.status = 'pending'
      and (c.expires_at is null or c.expires_at > now())
  ), '[]'::jsonb);
end;
$$;

create or replace function public.magpie_accept_workspace_claim(
  target_claim_id uuid,
  target_user_id uuid,
  target_email text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_email text := lower(trim(coalesce(target_email, '')));
  claim_row public.workspace_claims;
  workspace_row public.workspaces;
begin
  if not exists (
    select 1
    from auth.users u
    where u.id = target_user_id
      and lower(coalesce(u.email, '')) = normalized_email
  ) then
    return jsonb_build_object('claimed', false, 'error', 'account email does not match this claim');
  end if;

  select * into claim_row
  from public.workspace_claims c
  where c.id = target_claim_id
  for update;

  if claim_row.id is null
     or claim_row.status <> 'pending'
     or lower(claim_row.email) <> normalized_email
     or (claim_row.expires_at is not null and claim_row.expires_at <= now()) then
    return jsonb_build_object('claimed', false, 'error', 'workspace claim is invalid or expired');
  end if;

  select * into workspace_row
  from public.workspaces w
  where w.id = claim_row.workspace_id
  for update;

  if workspace_row.id is null then
    return jsonb_build_object('claimed', false, 'error', 'storage workspace no longer exists');
  end if;

  if claim_row.transfer_ownership then
    delete from public.workspace_members
    where workspace_id = workspace_row.id
      and user_id = workspace_row.owner_id;

    update public.workspaces
    set owner_id = target_user_id,
        updated_at = now()
    where id = workspace_row.id;

    insert into public.workspace_members(workspace_id, user_id, role)
    values (workspace_row.id, target_user_id, 'owner')
    on conflict (workspace_id, user_id)
    do update set role = excluded.role;
  else
    insert into public.workspace_members(workspace_id, user_id, role)
    values (workspace_row.id, target_user_id, claim_row.role)
    on conflict (workspace_id, user_id)
    do update set role = excluded.role;
  end if;

  update public.workspace_claims
  set status = 'claimed',
      claimed_by = target_user_id,
      claimed_at = now(),
      updated_at = now()
  where id = claim_row.id;

  return jsonb_build_object(
    'claimed', true,
    'claim_id', claim_row.id,
    'workspace', jsonb_build_object(
      'id', workspace_row.id,
      'name', workspace_row.name,
      'role', case when claim_row.transfer_ownership then 'owner' else claim_row.role end
    )
  );
end;
$$;

revoke execute on function public.magpie_pending_workspace_claims(uuid, text)
  from public, anon, authenticated;
revoke execute on function public.magpie_accept_workspace_claim(uuid, uuid, text)
  from public, anon, authenticated;
grant execute on function public.magpie_pending_workspace_claims(uuid, text)
  to service_role;
grant execute on function public.magpie_accept_workspace_claim(uuid, uuid, text)
  to service_role;

insert into public.workspace_claims(
  workspace_id,
  email,
  role,
  transfer_ownership
)
select
  w.id,
  'owner@example.org',
  'owner',
  true
from public.workspaces w
join auth.users owner_user on owner_user.id = w.owner_id
where w.name = 'Magpie'
  and lower(coalesce(owner_user.email, '')) = 'magpie-owner@example.org'
  and exists (
    select 1 from public.locations l where l.workspace_id = w.id
  )
  and not exists (
    select 1
    from public.workspace_claims c
    where c.workspace_id = w.id
      and lower(c.email) = 'owner@example.org'
      and c.status = 'pending'
  );

comment on table public.workspace_claims is
  'Email-addressed one-time offers to attach an existing storage workspace to a signed-in account.';
