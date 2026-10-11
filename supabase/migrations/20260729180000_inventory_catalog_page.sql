-- Return a bounded page of complete item groups for the shared inventory UI.
-- Paging item identities before hydrating placements prevents one item stored
-- in several places from being split across browser pages.

create or replace function public.magpie_natural_sort_key(input_text text)
returns text
language plpgsql
immutable
strict
set search_path = public
as $$
declare
  part text;
  result text := '';
begin
  for part in
    select token[1]
    from regexp_matches(input_text, '([0-9]+|[^0-9]+)', 'g')
      as matches(token)
  loop
    if part ~ '^[0-9]+$' then
      result := result || '0' || lpad(part, 20, '0');
    else
      result := result || '1' || lower(part);
    end if;
  end loop;
  return result;
end;
$$;

create or replace function public.magpie_inventory_catalog_page(
  target_workspace_id uuid,
  include_archived boolean default false,
  search_query text default '',
  category_filter text default '',
  scope_filter text default 'all',
  sort_mode text default 'name',
  page_limit integer default 50,
  page_offset integer default 0
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = public
as $$
declare
  clean_query text := lower(left(
    regexp_replace(trim(coalesce(search_query, '')), '\s+', ' ', 'g'),
    200
  ));
  clean_category text := lower(left(trim(coalesce(category_filter, '')), 100));
  clean_scope text := lower(coalesce(nullif(trim(scope_filter), ''), 'all'));
  clean_sort text := lower(coalesce(nullif(trim(sort_mode), ''), 'name'));
  clean_limit integer := greatest(1, least(coalesce(page_limit, 50), 100));
  clean_offset integer := greatest(0, coalesce(page_offset, 0));
  response jsonb;
begin
  if clean_scope not in ('all', 'review', 'photos', 'no-photo', 'uncategorized') then
    raise exception 'unknown inventory scope';
  end if;
  if clean_sort not in ('name', 'category', 'location', 'quantity') then
    raise exception 'unknown inventory sort';
  end if;

  with recursive location_tree as (
    select
      location.id,
      case when trim(location.label) = '' then location.local_code else location.label end
        as path_label,
      array[location.id] as trail
    from public.locations location
    where location.workspace_id = target_workspace_id
      and location.parent_id is null

    union all

    select
      child.id,
      parent.path_label || ' › ' ||
        case when trim(child.label) = '' then child.local_code else child.label end,
      parent.trail || child.id
    from public.locations child
    join location_tree parent on parent.id = child.parent_id
    where child.workspace_id = target_workspace_id
      and not child.id = any(parent.trail)
  ),
  workspace_items as (
    select item.*
    from public.items item
    where item.workspace_id = target_workspace_id
      and item.active = (not include_archived)
      and exists (
        select 1
        from public.inventory_entries entry
        where entry.workspace_id = target_workspace_id
          and entry.item_id = item.id
      )
  ),
  eligible as (
    select
      item.*,
      coalesce((
        select sum(entry.quantity)
        from public.inventory_entries entry
        where entry.workspace_id = target_workspace_id
          and entry.item_id = item.id
      ), 0) as total_quantity,
      coalesce((
        select min(coalesce(path.path_label, location.local_code))
        from public.inventory_entries entry
        join public.locations location on location.id = entry.location_id
        left join location_tree path on path.id = location.id
        where entry.workspace_id = target_workspace_id
          and entry.item_id = item.id
      ), '') as first_location
    from workspace_items item
    where
      (clean_category = '' or lower(trim(item.category)) = clean_category)
      and (
        clean_query = ''
        or lower(item.name) like '%' || clean_query || '%'
        or lower(item.category) like '%' || clean_query || '%'
        or lower(item.description) like '%' || clean_query || '%'
        or exists (
          select 1
          from public.item_aliases alias
          where alias.item_id = item.id
            and lower(alias.alias) like '%' || clean_query || '%'
        )
        or exists (
          select 1
          from public.inventory_entries entry
          join public.locations location on location.id = entry.location_id
          left join location_tree path on path.id = location.id
          where entry.workspace_id = target_workspace_id
            and entry.item_id = item.id
            and lower(coalesce(path.path_label, location.local_code))
              like '%' || clean_query || '%'
        )
      )
      and (
        clean_scope <> 'review'
        or exists (
          select 1
          from public.inventory_entries entry
          where entry.workspace_id = target_workspace_id
            and entry.item_id = item.id
            and entry.needs_review
        )
      )
      and (
        clean_scope <> 'uncategorized'
        or trim(item.category) = ''
      )
      and (
        clean_scope not in ('photos', 'no-photo')
        or (
          (
            exists (
              select 1
              from public.photos photo
              where photo.workspace_id = target_workspace_id
                and photo.item_id = item.id
            )
            or exists (
              select 1
              from public.inventory_entries entry
              join public.inventory_review_photos review_photo
                on review_photo.inventory_entry_id = entry.id
              where entry.workspace_id = target_workspace_id
                and entry.item_id = item.id
            )
          ) = (clean_scope = 'photos')
        )
      )
  ),
  ranked as (
    select
      eligible.*,
      row_number() over (
        order by
          case when clean_sort = 'quantity' then total_quantity end desc,
          case when clean_sort = 'category' then
            public.magpie_natural_sort_key(
              case when trim(category) = '' then 'Uncategorized' else category end
            )
          end,
          case when clean_sort = 'location' then
            public.magpie_natural_sort_key(first_location)
          end,
          public.magpie_natural_sort_key(name),
          id
      ) as page_order
    from eligible
  ),
  page as (
    select *
    from ranked
    where page_order > clean_offset
      and page_order <= clean_offset + clean_limit
  ),
  catalog_metrics as (
    select
      count(distinct item.id)::integer as items,
      count(entry.id)::integer as placements,
      count(distinct entry.location_id)::integer as locations,
      count(distinct item.id) filter (where entry.needs_review)::integer
        as need_review
    from workspace_items item
    join public.inventory_entries entry
      on entry.workspace_id = target_workspace_id
     and entry.item_id = item.id
  ),
  catalog_categories as (
    select distinct trim(item.category) as category
    from workspace_items item
    where trim(item.category) <> ''
  )
  select jsonb_build_object(
    'total', (select count(*) from eligible),
    'metrics', jsonb_build_object(
      'items', coalesce((select items from catalog_metrics), 0),
      'placements', coalesce((select placements from catalog_metrics), 0),
      'locations', coalesce((select locations from catalog_metrics), 0),
      'need_review', coalesce((select need_review from catalog_metrics), 0)
    ),
    'categories', coalesce((
      select jsonb_agg(category order by lower(category))
      from catalog_categories
    ), '[]'::jsonb),
    'items', coalesce((
      select jsonb_agg(
        jsonb_build_object(
          'item_id', item.local_id,
          'name', item.name,
          'description', item.description,
          'category', item.category,
          'aliases', coalesce((
            select jsonb_agg(alias.alias order by lower(alias.alias))
            from public.item_aliases alias
            where alias.item_id = item.id
          ), '[]'::jsonb),
          'bin_id', location.local_code,
          'quantity', entry.quantity,
          'unit', entry.unit,
          'needs_review', entry.needs_review,
          'review_reason', entry.review_reason,
          'photo_ids', coalesce((
            select jsonb_agg(photo_id order by photo_id)
            from (
              select photo.local_id as photo_id
              from public.photos photo
              where photo.workspace_id = target_workspace_id
                and photo.item_id = item.id
              union
              select photo.local_id
              from public.inventory_review_photos review_photo
              join public.photos photo on photo.id = review_photo.photo_id
              where review_photo.workspace_id = target_workspace_id
                and review_photo.inventory_entry_id = entry.id
            ) item_photos
          ), '[]'::jsonb),
          'size_class', item.size_class,
          'size_source', item.size_source,
          'size_confidence', item.size_confidence,
          'dimensions', item.dimensions,
          'home_location_id', coalesce(home.local_code, ''),
          'last_seen_location_id', coalesce(last_seen.local_code, ''),
          'active', item.active,
          'archived_at', item.archived_at
        )
        order by page.page_order, public.magpie_natural_sort_key(location.local_code)
      )
      from page
      join public.items item on item.id = page.id
      join public.inventory_entries entry
        on entry.workspace_id = target_workspace_id
       and entry.item_id = item.id
      join public.locations location on location.id = entry.location_id
      left join public.locations home on home.id = item.home_location_id
      left join public.locations last_seen on last_seen.id = item.last_seen_location_id
    ), '[]'::jsonb)
  )
  into response;

  return response;
end;
$$;

revoke all on function public.magpie_natural_sort_key(text) from public, anon;
grant execute on function public.magpie_natural_sort_key(text)
  to authenticated, service_role;

revoke all on function public.magpie_inventory_catalog_page(
  uuid, boolean, text, text, text, text, integer, integer
) from public, anon;
grant execute on function public.magpie_inventory_catalog_page(
  uuid, boolean, text, text, text, text, integer, integer
) to authenticated, service_role;

notify pgrst, 'reload schema';
