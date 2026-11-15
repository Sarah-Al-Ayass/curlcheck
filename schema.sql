-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run.
create table if not exists public.members (
  username    text primary key,
  first_name  text not null,
  pw_hash     text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  quiz        jsonb,
  details     jsonb,
  routine     jsonb,
  feedback    jsonb
);
-- Only the app (using the secret service_role key) can read or write this table.
alter table public.members enable row level security;
