# Marketplace Hub v317 — Catalog Sharing Model

## Obiettivo
Separare definitivamente la proprietà dei cataloghi dal singolo Seller e modellare la condivisione SaaS a livello Tenant/Agency/Piattaforma.

## Modello
Ogni `supplier` e `price_list` conserva `owner_tenant_id` e `sharing_scope`.

Ambiti supportati:

- `tenant`: catalogo privato del tenant proprietario; tutti i Seller dello stesso tenant possono usarlo.
- `agency`: catalogo di un tenant Agency disponibile anche ai tenant cliente collegati tramite `agency_clients`.
- `platform`: catalogo globale disponibile a tutti i tenant; può essere impostato solo dal Platform Admin.

Per eccezioni controllate esiste `catalog_tenant_access`, che assegna `use` o `manage` a tenant specifici.

## Compatibilità con il gestionale esistente
`visibility` (`private/shared/global`) e `price_list_access` restano presenti e vengono mantenuti come mirror di compatibilità. I servizi legacy che lavorano ancora per Seller continuano quindi a funzionare mentre FastAPI usa il nuovo confine Tenant.

## Sicurezza PostgreSQL
`price_lists` e `suppliers` ricevono policy RLS specifiche per cataloghi condivisi. Un tenant può leggere un listino soltanto se:

1. lo possiede;
2. è globale di piattaforma;
3. ha un grant esplicito;
4. il catalogo è Agency e il tenant è un cliente attivo dell'Agency.

Le scritture restano riservate al tenant proprietario o al bypass esplicito di piattaforma.

## API
Sono disponibili endpoint FastAPI per leggere e modificare la policy di condivisione di listini e fornitori. Le operazioni di modifica richiedono ruolo `owner`, `admin` o `manager`; `platform` richiede Platform Admin.

## Migrazione
Al primo utilizzo:

- `owner_tenant_id` viene ricavato da `tenant_sellers`;
- i vecchi listini `global` diventano `platform`;
- le vecchie condivisioni Seller vengono convertite in grant Tenant;
- nessun listino, Seller, ordine o dato contabile viene cancellato.
