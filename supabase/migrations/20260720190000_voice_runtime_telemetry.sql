alter table public.voice_attempts
  add column if not exists device_id text not null default '',
  add column if not exists runtime_version text not null default '';
