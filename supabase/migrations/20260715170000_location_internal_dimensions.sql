-- Real internal dimensions let placement reject containers that cannot fit an item.
alter table public.locations
  add column if not exists internal_dimensions jsonb not null default '{}'::jsonb;

alter table public.locations
  drop constraint if exists locations_internal_dimensions_object;
alter table public.locations
  add constraint locations_internal_dimensions_object
  check (jsonb_typeof(internal_dimensions) = 'object');
