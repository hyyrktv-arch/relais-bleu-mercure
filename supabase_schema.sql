-- Relais Bleu Mercure — schéma Supabase
-- À coller dans Supabase > SQL Editor > New query > Run

create table if not exists teams (
  code        text primary key,            -- ex : 'bleu-mercure' (saisi dans l'agent et l'app)
  name        text not null,
  created_at  timestamptz default now()
);

create table if not exists laps (
  lap_id       text primary key,            -- 'g61:<id>' | 'agent:<pilote>:<session>:<tour>' | 'ibt:...'
  team_code    text not null references teams(code),
  driver       text not null,
  car          text,
  track        text,
  session_type text,                        -- Practice | Qualifying | Race
  lap_time     double precision,
  fuel_used    double precision,
  fuel_level   double precision,
  clean        boolean default true,
  start_time   timestamptz,
  raw          jsonb,
  imported_at  timestamptz default now()
);
create index if not exists laps_team_track_car on laps (team_code, track, car);
create index if not exists laps_team_driver on laps (team_code, driver);

-- Position live envoyée par l'agent pendant une session (une ligne par pilote, écrasée)
create table if not exists live (
  team_code     text not null references teams(code),
  driver        text not null,
  car           text,
  track         text,
  session_type  text,
  session_time  double precision,          -- secondes écoulées dans la session
  time_remain   double precision,          -- secondes restantes (si connu)
  lap           integer,
  fuel_level    double precision,
  last_lap_time double precision,
  on_pit_road   boolean,
  updated_at    timestamptz default now(),
  primary key (team_code, driver)
);

-- Sécurité : l'agent (clé anon) peut seulement insérer ; l'app (clé service) fait tout.
alter table teams enable row level security;
alter table laps  enable row level security;
alter table live  enable row level security;

drop policy if exists agent_insert_laps on laps;
create policy agent_insert_laps on laps for insert to anon with check (true);

drop policy if exists agent_upsert_live on live;
create policy agent_upsert_live on live for insert to anon with check (true);
drop policy if exists agent_update_live on live;
create policy agent_update_live on live for update to anon using (true) with check (true);

-- Première équipe
insert into teams (code, name) values ('bleu-mercure', 'Bleu Mercure Racing')
on conflict (code) do nothing;
