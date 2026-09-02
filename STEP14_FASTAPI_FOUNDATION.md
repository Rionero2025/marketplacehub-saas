# v314 — FastAPI Foundation

## Obiettivo
Separare definitivamente il backend applicativo dall'interfaccia Streamlit senza
riscrivere la business logic già verificata.

La nuova API chiama direttamente i boundary di `marketplace_core` introdotti nelle
v301-v313. Streamlit continua a funzionare e può essere mantenuto come interfaccia
legacy/Agency durante la migrazione al frontend React/Next.js.

## Runtime
Avvio sviluppo:

```bash
python tools/run_api.py
```

oppure:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Health check:

- `GET /health`
- `GET /ready`
- Swagger: `/docs`

## Autenticazione
`POST /api/v1/auth/login` usa gli utenti già presenti in `app_users`.

La password non viene conservata nel browser o nel DB delle sessioni. L'API genera
un token casuale ad alta entropia e nel database salva esclusivamente SHA-256 del
token. Il token può essere usato come `Authorization: Bearer ...` ed è anche
impostato come cookie `HttpOnly` per preparare il frontend web.

Variabili utili:

- `MARKETPLACE_HUB_API_SESSION_HOURS=12`
- `MARKETPLACE_HUB_API_REMEMBER_DAYS=30`
- `MARKETPLACE_HUB_COOKIE_SECURE=1`
- `MARKETPLACE_HUB_CORS_ORIGINS=https://app.example.it`
- `MARKETPLACE_HUB_API_DOCS=1`
- `MARKETPLACE_HUB_API_LOCAL_JOBS=0`

In produzione SaaS `MARKETPLACE_HUB_API_LOCAL_JOBS` resta `0`: i job sono eseguiti
da worker dedicati.

## Autorizzazione
La API applica due controlli prima di esporre i dati:

1. permesso area (`marketplace_orders`, `accounting`, `buybox`, `work_lists`, ecc.);
2. Seller assegnati all'utente.

Un Seller fuori scope restituisce 404, evitando anche di rivelarne l'esistenza.

## Endpoint iniziali
- Auth: login/logout/me
- Seller e account marketplace
- Ordini paginati + enqueue sincronizzazione
- Contabilità status + enqueue sync/costi
- Buy Box risultati paginati/summary
- Cataloghi/listini server-side + enqueue materializzazione
- Job status/list

Nessun endpoint restituisce `credentials_encrypted` o chiavi marketplace.

## Architettura risultante

```text
Streamlit legacy/Agency ─┐
                         ├── marketplace_core ── PostgreSQL/Object Storage/Redis
FastAPI /api/v1 ─────────┘                  └── background_jobs ── worker

React/Next.js (prossimo step) ── HTTP ──> FastAPI
```
