create or replace function public.hook_magpie_restrict_signup(event jsonb)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  email_address text := lower(trim(coalesce(event->'user'->>'email', '')));
  email_domain text := split_part(email_address, '@', 2);
begin
  if email_address = 'owner@example.org' or email_domain = 'example.com' then
    return '{}'::jsonb;
  end if;

  return jsonb_build_object(
    'error', jsonb_build_object(
      'http_code', 403,
      'message', 'Magpie is currently limited to approved accounts.'
    )
  );
end;
$$;

grant execute on function public.hook_magpie_restrict_signup(jsonb)
  to supabase_auth_admin;
grant usage on schema public to supabase_auth_admin;
revoke execute on function public.hook_magpie_restrict_signup(jsonb)
  from authenticated, anon, public;

comment on function public.hook_magpie_restrict_signup(jsonb) is
  'Allows Magpie beta signups for owner@example.org and the example.com domain.';
