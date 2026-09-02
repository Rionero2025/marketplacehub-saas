# Step 1 — Extract the application core from Streamlit

## Goal
Preserve every proven Marketplace Hub rule while removing Streamlit as the orchestration layer.

### Baseline
This development package was rebuilt from the v285 full cloud package and the production hotfix chain through v292. Production itself is **not** modified by this package.

## First completed vertical slice: Accounting
The following flow now goes through `marketplace_core.AccountingCore`:

- accounting environment and sync state;
- local/PostgreSQL cache summary;
- accounting catalog selection;
- incremental and full order synchronization;
- cost recalculation (EAN / SKU / composite SKU logic remains in the proven service);
- accounting row retrieval.

The Streamlit page supplies user input and renders results; it no longer coordinates those service calls directly.

## Why this matters
FastAPI and future workers can call the exact same `AccountingCore` API. The business implementation under `services/accounting.py` remains authoritative while we migrate safely.

## Static audit at Step 1
Run:

```bash
python tools/core_audit.py
```

The report is written to `CORE_AUDIT.json` and measures page size and remaining Streamlit coupling.

## Extraction order after this slice
1. Dashboard orchestration.
2. Generic marketplace order synchronization.
3. Buy Box execution and batching.
4. Packlink / tracking jobs.
5. Catalog ingestion and product creation.
6. Replace direct DB calls in UI with repository/use-case contracts.

Only after parity do we place FastAPI endpoints and async workers in front of these use-cases.
