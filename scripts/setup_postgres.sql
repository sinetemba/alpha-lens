-- AlphaLens PostgreSQL database bootstrap.
--
-- Run as the postgres superuser after installing PostgreSQL, e.g.:
--   psql -U postgres -f scripts/setup_postgres.sql
--
-- This creates an 'alphalens' user and database with the correct privileges.

CREATE USER alphalens WITH PASSWORD 'changeme';
CREATE DATABASE alphalens OWNER alphalens;
GRANT ALL PRIVILEGES ON DATABASE alphalens TO alphalens;

\c alphalens
GRANT ALL ON SCHEMA public TO alphalens;
