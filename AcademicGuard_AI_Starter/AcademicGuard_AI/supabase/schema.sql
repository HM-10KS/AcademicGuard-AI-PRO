create extension if not exists pgcrypto;
create table if not exists public.scans (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references auth.users(id) on delete cascade,
 filename text not null,
 file_type text not null check (file_type in ('research','code')),
 score numeric(5,2) not null default 0,
 matched_sources integer not null default 0,
 report jsonb not null default '{}'::jsonb,
 created_at timestamptz not null default now()
);
create index if not exists scans_user_created_idx on public.scans(user_id,created_at desc);
alter table public.scans enable row level security;
drop policy if exists "users view own scans" on public.scans;
create policy "users view own scans" on public.scans for select to authenticated using ((select auth.uid()) = user_id);
-- Writes happen through the authenticated backend using the service role key.
