alter table public.locations
  add column if not exists accepts_items boolean not null default true;

-- Existing rows predate the explicit destination flag. Preserve the product's
-- type defaults instead of making every room, cabinet, or rack a put-away
-- destination during upgrade.
update public.locations
set accepts_items = false
where type in (
  'home', 'room', 'area', 'closet', 'cabinet', 'shelf-unit', 'rack',
  'storage_unit'
);

comment on column public.locations.accepts_items is
  'Whether inventory may be stored directly at this node instead of only below it.';
