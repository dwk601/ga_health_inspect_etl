# ga_health_inspect

ETL pipeline from Georgia health inspection web data to PostgreSQL.
Source: https://ga.healthinspections.us/stateofgeorgia/
Stack: Python, PostgreSQL, Docker.
Scope is extraction, transformation, and loading—not a user-facing application. Additional product features are not specified.

## Load-bearing gotchas

- Source routes, pagination, record identifiers, and field semantics are unverified. Inspect the source before encoding assumptions about them.
- PostgreSQL is the destination, not the source of truth for what the website exposes. Do not infer source deletions from an incomplete or failed extraction.
- Destructive database operations (dropping, truncating, or replacing existing data) require explicit user authorization for the target database.
- The local volume `ga_health_inspect_data` (default Compose project) holds a stopped copy of the data. Never run `docker compose` here without an explicit scratch `-p`, and don't rely on env vars or traps persisting across shell calls.
- `schema.sql` runs on every start against existing databases: changes must be additive and idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE OR REPLACE VIEW`); no new `etl_runs` statuses.
- verify: with an unused scratch name, run `POSTGRES_PASSWORD=scratch-only docker compose -p ga-check-<name> up -d --build --wait`, then `... -p ga-check-<name> exec -T etl python -m ga_health_inspect --max-pages 2 --reports --max-reports 5`; expect a one-line summary with status=bounded, next_page=2, 10 facilities and 5 parsed_reports. Run `... -p ga-check-<name> exec -T etl python -m unittest discover -s tests -v`; clean up with `... -p ga-check-<name> down -v --rmi local` and confirm `ga_health_inspect_data` still exists.

## Deployment

- Coolify app `ga-health-inspect-etl` (uuid `iejqjmufmnpmpyswfznrgzr2`) on `root@100.92.189.13` runs this compose file; Postgres is inside it, not a Coolify database resource, so Coolify backups don't cover it.
- Scheduled task `daily-etl` runs in the idle `etl` container at `0 8 * * *` UTC: `timeout -s INT -k 120 35000 python -m ga_health_inspect --reports` (Coolify task timeout 36000s must stay above it). A full pass from page 0 takes about 1.5h.
- Host systemd timer `ga-health-backup.timer` (07:15 UTC) runs `/usr/local/sbin/ga-health-backup`: `pg_dump -Fc` to `/var/backups/ga-health-inspect/health-*.dump`, kept 7 days, on the same host. Restoring a dump is preferred over a re-crawl.
- After deploying a parser fix, run `python -m ga_health_inspect --retry-report-errors` once; stored report errors are otherwise never re-fetched.

## Standing rules

- Write clean code: clear names, small focused functions, no dead code or speculative abstractions.
- Never save docs or plans to the repo (no README additions, docs/, plan files, decision logs, or summaries) unless the user explicitly asks.
- Write as few tests as possible. Tests must not slow development. No unit tests for implementation details.
- Before adding a test, answer: What observable behavior or contract does it protect? What credible regression fails it? Why doesn't existing coverage already catch that? Does it need a production seam (export, flag, wrapper) no real caller needs? Any missing answer means don't add it.
- Each contract gets one test, at the strongest boundary. Extend an existing case or table instead of adding a near-duplicate. Never add: assertion-free coverage probes, mocks that implement the behavior under test, expected values computed by the code under test, copied fixtures or export lists, source or string greps, private-helper tests, or test-only exports.
- A test that breaks under a behavior-preserving refactor asserts implementation; rewrite it at the real boundary or delete it.
- Bug fix: one regression test at the owning boundary, and it must fail on the pre-fix code.
- While developing, run only the focused test for the change. Run the full suite once before handoff.

## Skill map

No project skills yet. Source extraction, transformation, PostgreSQL loading, and Docker operation have too little established project-specific knowledge to warrant separate skills. Use the context above; split an area into an on-demand skill when concrete interfaces, pitfalls, and verification justify it.

## Maintain

Prune instead of append; point instead of copy. Keep each rule in one place. When an area earns a skill, move its detailed rules there and leave only its name and load trigger here.
