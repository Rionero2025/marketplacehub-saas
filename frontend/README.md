# Marketplace Hub Frontend — v323 staging

Frontend SaaS Next.js/React/TypeScript sopra FastAPI.

## Sviluppo
```bash
npm install
npm run dev
```

## Verifica
```bash
npm run typecheck
npm run build
```

Il frontend usa `/api/v1/...` sullo stesso origin; `next.config.ts` inoltra le API al backend configurato con `MARKETPLACE_HUB_API_ORIGIN`.

v322 aggiunge Dashboard Pro, sidebar organizzata, workspace switcher, monitor job in topbar, onboarding visuale e layout responsive.


v323 aggiunge il wiring Render staging tramite rete privata verso FastAPI.
