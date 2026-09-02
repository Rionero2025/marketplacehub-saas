# Marketplace Hub SaaS v323 — Staging Deploy

Questa release non modifica la produzione `marketplacehub-1`.

## Risorse Render create dal nuovo `render.yaml`

- `marketplacehub-saas-web-staging` — Next.js / React
- `marketplacehub-saas-api-staging` — FastAPI
- `marketplacehub-saas-worker-staging` — worker Python persistente
- `marketplacehub-saas-db-staging` — PostgreSQL separato
- `marketplacehub-saas-cache-staging` — Render Key Value / Redis-compatible

Tutti i nomi sono separati dalla produzione.

## Prima della creazione del Blueprint

Genera una nuova master key **solo per lo staging** sul tuo PC:

```bash
python tools/generate_master_key.py
```

Conserva il valore e incollalo, identico, nei due campi
`MARKETPLACE_HUB_MASTER_KEY` richiesti da Render per API e Worker.
Non inserire la chiave nel repository GitHub.

Scegli inoltre un codice invito staging e inseriscilo nel campo
`MARKETPLACE_HUB_SIGNUP_INVITE_CODE` dell'API. Servirà nella schermata di
registrazione.

## Deploy

1. Render Dashboard → **New + → Blueprint**.
2. Collega il repository privato `marketplacehub-saas`.
3. Branch `main`.
4. Blueprint path: `render.yaml`.
5. Controlla che i nomi delle risorse terminino tutti con `-staging`.
6. Inserisci i secret richiesti.
7. Approva la creazione delle risorse.

Il database staging è volutamente separato e parte vuoto. Non importare il
PostgreSQL della produzione in questa fase.

## Primo accesso

Quando frontend e API risultano `Deployed`:

1. apri l'URL di `marketplacehub-saas-web-staging`;
2. scegli **Crea account**;
3. usa il codice invito scelto durante il deploy;
4. crea azienda, Owner e primo Seller;
5. entra nella Dashboard.

Il trial viene creato dal Billing Engine interno; Stripe non è necessario.

## Verifiche

API:

- `/health` — processo API vivo
- `/ready` — database/cache/storage raggiungibili
- `/docs` — OpenAPI staging

Frontend:

- `/login`
- `/signup`
- `/dashboard`

## Costi / note Render

Il Blueprint reale usa un worker sempre attivo e il più piccolo PostgreSQL
persistente: queste due risorse sono a pagamento. API e frontend sono impostati
sul piano Free e possono avere cold start. Render Key Value è Free e viene usato
come cache, quindi la perdita della cache non perde dati applicativi.

Per un semplice smoke test senza misurazioni prestazionali puoi temporaneamente
sospendere il worker dal Dashboard, sapendo che i job resteranno in coda finché
non lo riattivi.

## Object Storage

v313 ha già astratto i file durevoli, ma v323 lascia
`MARKETPLACE_HUB_STORAGE_BACKEND=local` perché non sono ancora state configurate
credenziali S3/R2/GCS. Questo va bene per il primo staging; prima della produzione
SaaS collegheremo uno storage condiviso.
