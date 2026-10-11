-- Archive and restore complete location subtrees in one transaction. This
-- prevents network failures or a descendant name conflict from leaving only
-- part of a physical storage path active.
create or replace function public.magpie_set_location_subtree_active(
  target_workspace_id uuid,
  root_location_code text,
  target_active boolean
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  root_location public.locations%rowtype;
  target_ids uuid[];
  target_codes text[];
begin
  select location.*
  into root_location
  from public.locations location
  where location.workspace_id = target_workspace_id
    and location.local_code = root_location_code;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'unknown location';
  end if;

  if not target_active and not root_location.active then
    raise exception using
      errcode = 'P0002',
      message = 'unknown active location';
  end if;

  if target_active and root_location.parent_id is not null and exists (
    select 1
    from public.locations parent
    where parent.id = root_location.parent_id
      and not parent.active
  ) then
    raise exception using
      errcode = '23514',
      message = 'restore the parent location first';
  end if;

  with recursive subtree as (
    select location.id, location.local_code, location.parent_id, location.label,
      0 as depth, array[location.local_code]::text[] as sort_path
    from public.locations location
    where location.id = root_location.id

    union all

    select child.id, child.local_code, child.parent_id, child.label,
      subtree.depth + 1, subtree.sort_path || child.local_code
    from public.locations child
    join subtree on child.parent_id = subtree.id
    where child.workspace_id = target_workspace_id
  )
  select
    array_agg(subtree.id order by subtree.depth, subtree.sort_path),
    array_agg(subtree.local_code order by subtree.depth, subtree.sort_path)
  into target_ids, target_codes
  from subtree;

  if target_active and exists (
    select 1
    from public.locations restoring
    where restoring.id = any(target_ids)
    group by restoring.parent_id,
      lower(regexp_replace(btrim(restoring.label), '[[:space:]]+', ' ', 'g'))
    having count(*) > 1
  ) then
    raise exception using
      errcode = '23505',
      message = 'an active storage place with that name already exists at this level';
  end if;

  if target_active and exists (
    select 1
    from public.locations restoring
    join public.locations sibling
      on sibling.workspace_id = restoring.workspace_id
      and sibling.parent_id is not distinct from restoring.parent_id
      and sibling.active
      and sibling.id <> all(target_ids)
      and lower(regexp_replace(btrim(sibling.label), '[[:space:]]+', ' ', 'g'))
        = lower(regexp_replace(btrim(restoring.label), '[[:space:]]+', ' ', 'g'))
    where restoring.id = any(target_ids)
  ) then
    raise exception using
      errcode = '23505',
      message = 'an active storage place with that name already exists at this level';
  end if;

  update public.locations location
  set active = target_active,
      archived_at = case when target_active then null else now() end
  where location.id = any(target_ids);

  return jsonb_build_object('location_ids', to_jsonb(target_codes));
end;
$$;
