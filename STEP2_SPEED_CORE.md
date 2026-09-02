# Marketplace Hub v302 — Performance / Speed Core

Baseline: v301 Core extraction. Production repository remains untouched.

## Real optimizations in this step

1. **Dashboard: one accounting read instead of two**
   - KPI summaries, Top 10 and card details share one snapshot.
   - The SQL projection excludes `raw_json`, tracking/customer blobs and other columns unused by the Dashboard.

2. **Manual accounting overrides: one query instead of N+1**
   - All account/marketplace scopes are loaded in one batched query and indexed in memory.

3. **Authentication reruns: throttled DB refresh**
   - Streamlit no longer queries `app_users` for every widget rerun.
   - Default authorization refresh interval: 10 seconds (`MARKETPLACE_HUB_SESSION_REFRESH_SECONDS`).

4. **Redis-ready Core cache contract**
   - `marketplace_core/performance.py` introduces a Streamlit-independent TTL cache interface.
   - This is infrastructure for the next worker/API steps, not a replacement for PostgreSQL.

## Compatibility

- Business formulas are unchanged.
- Marketplace integrations are unchanged.
- Existing functions `dashboard_summaries()` and `dashboard_detail_rows()` remain available as compatibility wrappers.
- No database destructive migration.
