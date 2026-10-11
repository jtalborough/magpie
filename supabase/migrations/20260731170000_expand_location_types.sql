-- Represent common household and workshop storage without forcing users to
-- misclassify closets and portable or compartmented containers.

alter table public.locations drop constraint if exists locations_type_check;
alter table public.locations
  add constraint locations_type_check check (
    type in (
      'home', 'room', 'area', 'closet', 'cabinet', 'drawer', 'shelf',
      'shelf-unit', 'tray', 'bag', 'organizer', 'box', 'pouch', 'bin',
      'cell', 'tote', 'rack', 'locker', 'storage_unit'
    )
  );
