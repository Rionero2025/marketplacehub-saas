# v320 — SaaS Self-Service Onboarding

Marketplace Hub può ora creare un nuovo cliente SaaS senza interventi manuali.

Flusso:
1. scelta di un piano pubblico;
2. registrazione account Owner;
3. creazione Tenant Merchant;
4. creazione e assegnazione del primo Seller;
5. avvio trial provider-independent;
6. emissione sessione browser HttpOnly;
7. collegamento Kaufland o Worten con credenziali cifrate;
8. stato onboarding consultabile via API.

La registrazione pubblica è disattivata di default. Abilitarla con `MARKETPLACE_HUB_PUBLIC_SIGNUP=1`. Se `MARKETPLACE_HUB_SIGNUP_INVITE_CODE` è valorizzato, il codice è obbligatorio. Stripe non è richiesto.

Endpoint:
- `GET /api/v1/onboarding/plans`
- `POST /api/v1/onboarding/signup`
- `GET /api/v1/onboarding/status`
- `POST /api/v1/onboarding/marketplaces`
