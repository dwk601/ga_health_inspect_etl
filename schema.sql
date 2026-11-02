CREATE TABLE IF NOT EXISTS facilities (
 source_id text PRIMARY KEY, name text, address text, phone text, permit_type text,
 permit_number text, last_inspection_date date, last_inspection_score numeric,
 raw jsonb NOT NULL, first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inspections (
 facility_id text NOT NULL REFERENCES facilities(source_id), source_id bigint NOT NULL,
 report_template text NOT NULL, inspection_date date, purpose text, score numeric, inspector text,
 printable_url text, raw jsonb NOT NULL, violations_hash text NOT NULL,
 report_fields jsonb, report_raw jsonb, report_fetched_at timestamptz, report_error text,
 PRIMARY KEY (facility_id, source_id)
);
CREATE TABLE IF NOT EXISTS violations (
 facility_id text NOT NULL, inspection_id bigint NOT NULL, snapshot_hash text NOT NULL, source_key text NOT NULL,
 code_title text, regulation text, points numeric, corrected boolean, repeat boolean, notes text, raw jsonb NOT NULL,
 PRIMARY KEY (facility_id, inspection_id, snapshot_hash, source_key),
 FOREIGN KEY (facility_id, inspection_id) REFERENCES inspections(facility_id, source_id)
);
CREATE OR REPLACE VIEW current_violations AS
 SELECT v.* FROM violations v JOIN inspections i
 ON v.facility_id = i.facility_id AND v.inspection_id = i.source_id
 AND v.snapshot_hash = i.violations_hash;
CREATE TABLE IF NOT EXISTS etl_runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, started timestamptz NOT NULL DEFAULT now(), finished timestamptz,
 status text NOT NULL CHECK (status IN ('running','bounded','exhausted','caught_up','failed','interrupted')),
 options jsonb NOT NULL, next_page integer NOT NULL, counters jsonb NOT NULL DEFAULT '{}'::jsonb,
 conversion_errors integer NOT NULL DEFAULT 0, report_errors integer NOT NULL DEFAULT 0, error text
);
DO $$
DECLARE constraint_name text;
BEGIN
 FOR constraint_name IN
   SELECT conname FROM pg_constraint
   WHERE conrelid = 'etl_runs'::regclass AND contype = 'c'
     AND pg_get_constraintdef(oid) LIKE '%status%'
     AND pg_get_constraintdef(oid) NOT LIKE '%caught_up%'
 LOOP
   EXECUTE format('ALTER TABLE etl_runs DROP CONSTRAINT %I', constraint_name);
   EXECUTE format('ALTER TABLE etl_runs ADD CONSTRAINT %I CHECK (status IN (''running'', ''bounded'', ''exhausted'', ''caught_up'', ''failed'', ''interrupted''))', constraint_name);
 END LOOP;
END $$;
