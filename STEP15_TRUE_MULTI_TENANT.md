# Marketplace Hub v315 — True Multi-Tenant Foundation

## Obiettivo
Separare definitivamente il concetto di cliente SaaS dal `seller_id` operativo.

La gerarchia introdotta è:

- Tenant `merchant`: singolo cliente SaaS / azienda.
- Tenant `agency`: agenzia che può gestire più tenant merchant.
- Utenti: membri di uno o più tenant con ruolo.
- Seller: appartiene a un solo tenant proprietario.
- Marketplace account: resta figlio del Seller, quindi eredita il confine tenant.

## Compatibilità con l'installazione attuale
Al primo avvio v315 crea un tenant `Marketplace Hub Agency` e vi associa una sola volta gli utenti e Seller già esistenti. Questo conserva l'installazione attuale come versione Agency senza modificare i dati operativi.

## Isolamento API
Ogni sessione API possiede un `active_tenant_id`. Anche il Platform Admin vede i Seller soltanto del tenant attivo: per lavorare su un altro cliente deve cambiare esplicitamente contesto tenant.

Il vecchio filtro Seller per utente rimane valido come restrizione aggiuntiva: può restringere il tenant, mai ampliarlo.

## Agency
Una Agency può essere collegata a più tenant `merchant`. Gli utenti membri della Agency possono vedere i tenant clienti collegati e passare esplicitamente da uno all'altro. Il Seller del cliente non viene copiato nell'Agency.

## Tabelle aggiunte
- `tenants`
- `tenant_memberships`
- `tenant_sellers`
- `agency_clients`
- `tenancy_meta`

`api_sessions` riceve `active_tenant_id`.

## API aggiunte
- `GET /api/v1/tenants`
- `POST /api/v1/tenants/{tenant_id}/activate`
- `GET /api/v1/tenants/{tenant_id}/sellers`
- endpoint Platform Admin per creare tenant, membership, assegnare/trasferire Seller e collegare clienti a una Agency.

## Prossimo passo
v316: propagazione `tenant_id` alle tabelle operative principali + policy PostgreSQL/RLS e guard centralizzato per ottenere isolamento anche a livello database, non soltanto API.
