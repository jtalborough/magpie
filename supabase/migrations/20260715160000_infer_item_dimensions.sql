-- Infer metric hardware dimensions from item names without replacing measurements.
-- The trigger covers PostgREST clients and the atomic Pi put-away RPC alike.
create or replace function public.magpie_infer_item_dimensions(item_name text)
returns jsonb
language plpgsql
immutable
security definer
set search_path = public
as $$
declare
  metric_match text[];
  thread_match text[];
begin
  metric_match := regexp_match(
    coalesce(item_name, ''),
    '[mM][[:space:]]*([0-9]+([.][0-9]+)?)[[:space:]]*[xX×][[:space:]]*([0-9]+([.][0-9]+)?)([[:space:]]*mm)?'
  );
  if metric_match is not null then
    return jsonb_build_object(
      'thread_diameter_mm', metric_match[1]::numeric,
      'length_mm', metric_match[3]::numeric
    );
  end if;

  thread_match := regexp_match(
    coalesce(item_name, ''),
    '[mM][[:space:]]*([0-9]+([.][0-9]+)?)'
  );
  if thread_match is not null then
    return jsonb_build_object('thread_diameter_mm', thread_match[1]::numeric);
  end if;
  return '{}'::jsonb;
end;
$$;

create or replace function public.magpie_set_inferred_item_dimensions()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if coalesce(new.dimensions, '{}'::jsonb) = '{}'::jsonb then
    new.dimensions := public.magpie_infer_item_dimensions(new.name);
  elsif tg_op = 'UPDATE'
    and new.name is distinct from old.name
    and new.dimensions is not distinct from old.dimensions
    and old.dimensions = public.magpie_infer_item_dimensions(old.name)
  then
    new.dimensions := public.magpie_infer_item_dimensions(new.name);
  end if;
  return new;
end;
$$;

drop trigger if exists set_magpie_inferred_item_dimensions on public.items;
create trigger set_magpie_inferred_item_dimensions
before insert or update of name on public.items
for each row execute function public.magpie_set_inferred_item_dimensions();

update public.items
set dimensions = public.magpie_infer_item_dimensions(name)
where dimensions = '{}'::jsonb
  and public.magpie_infer_item_dimensions(name) <> '{}'::jsonb;

revoke all on function public.magpie_infer_item_dimensions(text) from public, anon, authenticated;
revoke all on function public.magpie_set_inferred_item_dimensions() from public, anon, authenticated;
grant execute on function public.magpie_infer_item_dimensions(text) to service_role;
grant execute on function public.magpie_set_inferred_item_dimensions() to service_role;
