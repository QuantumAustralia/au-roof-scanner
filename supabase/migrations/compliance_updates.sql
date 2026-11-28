-- Marketplace consent fields and the public-listing suppression list.
-- Run this in the Supabase SQL editor for the QMS Roof Scan project.

create table if not exists public.orders (
  order_id text primary key,
  email text,
  amount_ex_gst integer,
  status text,
  created_at timestamptz default now()
);

alter table public.orders
  add column if not exists terms_accepted_at timestamptz,
  add column if not exists terms_version text,
  add column if not exists buyer_ip text,
  add column if not exists buyer_user_agent text;

create table if not exists public.suppressed_properties (
  id bigint generated always as identity primary key,
  street_address text not null,
  suburb text not null,
  postcode text not null,
  reason text,
  created_at timestamptz not null default now()
);

create index if not exists suppressed_properties_address_idx
  on public.suppressed_properties (postcode, suburb, street_address);
