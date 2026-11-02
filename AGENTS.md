# ga_health_inspect

ETL pipeline from Georgia health inspection web data to PostgreSQL.
Source: https://ga.healthinspections.us/stateofgeorgia/
Stack: Python, PostgreSQL, Docker.
Scope is extraction, transformation, and loading—not a user-facing application. Additional product features are not specified.

## Load-bearing gotchas

- Source routes, pagination, record identifiers, and field semantics are unverified. Inspect the source before encoding assumptions about them.
- PostgreSQL is the destination, not the source of truth for what the website exposes. Do not infer source deletions from an incomplete or failed extraction.
- Destructive database operations (dropping, truncating, or replacing existing data) require explicit user authorization for the target database.
- verify: set `POSTGRES_PASSWORD` and `COMPOSE_PROJECT_NAME` for a scratch Compose project, then run `docker compose up -d --wait && docker compose exec etl python -m ga_health_inspect --max-pages 2 --reports --max-reports 5`; expect a summary with status=bounded, next_page=2, 10 facilities and 5 parsed_reports. Run `docker compose exec etl python -m unittest discover -s tests -v`; clean up the scratch project with `docker compose down -v --rmi local`.

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
