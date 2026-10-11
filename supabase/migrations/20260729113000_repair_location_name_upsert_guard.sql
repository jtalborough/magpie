-- PostgREST upserts run BEFORE INSERT triggers before resolving the
-- (workspace_id, local_code) conflict. Exclude that logical row so metadata
-- updates do not conflict with their own active name.
create or replace function public.magpie_guard_active_sibling_location_name()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  normalized_name text;
  old_normalized_name text;
  lock_scope text;
begin
  if not new.active then
    return new;
  end if;

  normalized_name := lower(
    regexp_replace(btrim(new.label), '[[:space:]]+', ' ', 'g')
  );

  if tg_op = 'UPDATE' then
    old_normalized_name := lower(
      regexp_replace(btrim(old.label), '[[:space:]]+', ' ', 'g')
    );
    if new.workspace_id is not distinct from old.workspace_id
      and new.parent_id is not distinct from old.parent_id
      and new.active is not distinct from old.active
      and normalized_name = old_normalized_name
    then
      return new;
    end if;
  end if;

  lock_scope := new.workspace_id::text
    || ':' || coalesce(new.parent_id::text, '<root>')
    || ':' || normalized_name;
  perform pg_advisory_xact_lock(hashtextextended(lock_scope, 0));

  if exists (
    select 1
    from public.locations sibling
    where sibling.workspace_id = new.workspace_id
      and sibling.parent_id is not distinct from new.parent_id
      and sibling.active
      and sibling.id <> new.id
      and sibling.local_code <> new.local_code
      and lower(
        regexp_replace(btrim(sibling.label), '[[:space:]]+', ' ', 'g')
      ) = normalized_name
  ) then
    raise exception using
      errcode = '23505',
      message = 'an active storage place with that name already exists at this level';
  end if;

  return new;
end;
$$;

drop trigger if exists guard_active_sibling_location_name on public.locations;
create trigger guard_active_sibling_location_name
before insert or update of workspace_id, parent_id, label, active
on public.locations
for each row
execute function public.magpie_guard_active_sibling_location_name();
