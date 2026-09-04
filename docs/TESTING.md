# Core regression tests

Install application and test dependencies with:

```sh
python -m pip install -r requirements-test.txt
python -m compileall -q app.py pages services tools api marketplace_core
python -m pytest -q tests_core
```

GitHub Actions runs these checks on pull requests and pushes to main with Python
3.12. Test dependencies are separate from runtime requirements.

The onboarding registration test uses a private in-memory SQLite database and
cache. Production services create the user, tenant, membership, seller link,
subscription and completion event. Two registrations must retain separate IDs
and associations, an owner membership, a non-admin user and a 14-day trial.
The fixture supplies only the legacy tables/columns needed by this workflow;
the service initializers create the user, tenancy and billing schemas.

This is a persistence regression test, not a PostgreSQL RLS, migration or browser
test. A separate PostgreSQL suite is needed to verify isolation under real RLS.
The remaining source-contract tests in this file have not been converted.

Local validation on 2026-09-04: 77 core tests passed on Python 3.13/Windows;
compileall and git diff --check passed. A temporary process-only mutation which
skipped the seller-to-tenant link made the new test fail at the missing persisted
association. No production source was edited for that mutation. GitHub's Linux
run must still be checked after publication.
