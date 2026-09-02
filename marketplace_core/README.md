# Marketplace Core — Step 1

`marketplace_core` is the application boundary between UI and the proven Marketplace Hub domain logic.

Rules:

1. **No Streamlit imports** in this package.
2. Pages render UI; they do not orchestrate marketplace workflows directly.
3. Existing `services/*` remain the authoritative implementation during migration.
4. FastAPI and workers will call the same Core use-cases.
5. A feature is considered extracted only when Streamlit calls a Core method instead of coordinating several service calls itself.

The first extracted vertical slice is **Accounting**: state/cache, catalog selection, incremental/full synchronization, cost refresh and row retrieval.

## v303 Orders Core

`orders.py` adds a bounded, UI-independent order query contract. List views must use
`OrderQuery(limit, offset, filters...)` rather than loading complete order histories.
