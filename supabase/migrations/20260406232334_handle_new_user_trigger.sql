-- Migration: Create trigger to auto-provision public.users + public.organizations
-- when a new user signs up via Supabase Auth.
--
-- The trigger extracts first_name, last_name, organization_name from
-- raw_user_meta_data (set by the frontend during supabase.auth.signUp()).

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  org_id uuid;
  org_name text;
  base_org_name text;
  fname text;
  lname text;
  suffix int := 0;
BEGIN
  -- Skip if user already exists in public.users (idempotency guard)
  IF EXISTS (SELECT 1 FROM public.users WHERE id = NEW.id) THEN
    RETURN NEW;
  END IF;

  fname := COALESCE(NEW.raw_user_meta_data->>'first_name', '');
  lname := COALESCE(NEW.raw_user_meta_data->>'last_name', '');
  base_org_name := COALESCE(
    NULLIF(TRIM(NEW.raw_user_meta_data->>'organization_name'), ''),
    fname || '''s Organization'
  );

  -- Find a unique organization name
  org_name := base_org_name;
  WHILE EXISTS (SELECT 1 FROM public.organizations WHERE name = org_name) LOOP
    suffix := suffix + 1;
    org_name := base_org_name || ' (' || suffix || ')';
  END LOOP;

  -- Create organization (FREE tier, 10 GB)
  INSERT INTO public.organizations (
    id, name, storage_tier, storage_used_bytes, storage_limit_bytes,
    is_active, is_deleted, created_at, updated_at
  )
  VALUES (
    gen_random_uuid(), org_name, 'FREE'::storagetier, 0, 10737418240,
    true, false, now(), now()
  )
  RETURNING id INTO org_id;

  -- Create user linked to organization
  INSERT INTO public.users (
    id, email, password_hash, first_name, last_name,
    role, is_active, is_deleted, organization_id,
    login_count, created_at, updated_at
  )
  VALUES (
    NEW.id, NEW.email, NEW.encrypted_password, fname, lname,
    'USER'::userrole, true, false, org_id,
    0, now(), now()
  );

  RETURN NEW;
END;
$$;

-- Fire after every new Supabase Auth signup
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();
