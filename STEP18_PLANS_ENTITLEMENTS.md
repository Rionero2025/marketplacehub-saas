# Marketplace Hub v318 — Plans & Entitlements Engine

## Obiettivo
Trasformare i pacchetti commerciali in regole applicative reali. Un limite SaaS non viene più applicato soltanto nascondendo una voce di menu: API, worker e operazioni di creazione consultano lo stesso motore di entitlement.

## Piani iniziali
La baseline commerciale attualmente codificata è:

- **Starter — €29/mese**: 1 marketplace, 1 fornitore.
- **Growth — €39/mese**: 1 marketplace, 3 fornitori.
- **Pro — €59/mese**: 2 marketplace, 3 fornitori.
- **Unlimited — €499/mese**: marketplace e fornitori senza limite applicativo.
- **Agency**: piano interno/non pubblico per la console Agency.
- **Legacy**: compatibilità non pubblica per tenant precedenti alla v318, per evitare regressioni durante la migrazione.

Limiti commerciali non ancora definiti (numero utenti, Seller, listini, ordini/mese, job/mese) restano volutamente `null`/illimitati finché non vengono fissati. Il motore è già predisposto per applicarli quando saranno decisi.

## Tabelle
- `saas_plans`
- `tenant_subscriptions`
- `tenant_entitlement_overrides`
- `tenant_usage_monthly`

Le ultime tre sono tenant-scoped e vengono protette da PostgreSQL RLS.

## Enforcement
### API
`require_permission()` ora verifica due condizioni distinte:
1. l'utente ha il permesso dell'area;
2. il piano del tenant abilita quella feature.

### Worker
Il job viene controllato sia quando viene accodato sia quando viene eseguito. Un downgrade/sospensione avvenuto mentre un job è in coda viene quindi rispettato dal worker.

### Risorse
Sono disponibili controlli centrali per:
- marketplace distinti;
- fornitori;
- utenti;
- Seller;
- listini;
- metriche mensili future.

La UI Streamlit transitoria usa già i controlli di capienza per nuovi marketplace, fornitori e listini. Il futuro frontend React userà gli stessi vincoli via FastAPI.

## API
- `GET /api/v1/plans`
- `GET /api/v1/tenants/{tenant_id}/entitlements`
- `PUT /api/v1/tenants/{tenant_id}/plan` — Platform Admin
- `PUT /api/v1/tenants/{tenant_id}/entitlements/{key}` — override Platform Admin
- `DELETE /api/v1/tenants/{tenant_id}/entitlements/{kind}/{key}` — rimozione override

## Billing
La tabella `tenant_subscriptions` contiene già gli identificatori esterni necessari per collegare successivamente Stripe. La v318 non effettua pagamenti: separa intenzionalmente il motore di entitlement dal provider di billing.
