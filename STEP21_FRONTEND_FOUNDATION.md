# Marketplace Hub v321 — Frontend Foundation

## Obiettivo
Separare il frontend SaaS da Streamlit e iniziare una UI Next.js/React che consumi esclusivamente le API FastAPI.

## Struttura
- `frontend/` — Next.js App Router + TypeScript
- reverse proxy `/api/*` verso FastAPI tramite `MARKETPLACE_HUB_API_INTERNAL_URL`
- cookie di sessione HttpOnly già emesso dal backend v314/v320
- tenant selector per utenti multi-tenant/Agency
- seller selector persistito nel browser per tenant
- menu filtrato dai permessi reali dell'utente

## Pagine v321
- `/login`
- `/signup`
- `/dashboard`
- `/orders`
- `/accounting`
- `/catalogs`
- `/buybox`
- `/jobs`
- `/settings`

## Performance
La UI non legge file locali né DataFrame completi. Utilizza endpoint paginati, cache backend e job in background già introdotti nelle v301-v320.

## Avvio sviluppo
Terminale 1:
`python tools/run_api.py`

Terminale 2:
`cd frontend && npm install && npm run dev`

Aprire `http://localhost:3000`.

### Nota cookie locale
In sviluppo HTTP locale usare `MARKETPLACE_HUB_COOKIE_SECURE=0`. In produzione deve restare `1` dietro HTTPS.
