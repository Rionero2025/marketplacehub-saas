# Marketplace Hub v316 — Tenant Database Isolation + PostgreSQL RLS

## Obiettivo
Aggiungere un secondo confine di sicurezza sotto FastAPI: il database PostgreSQL deve impedire letture/scritture cross-tenant anche se una query applicativa dimentica accidentalmente un filtro.

## Come funziona
- ogni richiesta API autenticata apre un `tenant_database_scope(active_tenant_id)`;
- ogni transazione PostgreSQL riceve `marketplace_hub.tenant_id` tramite `set_config(..., true)`;
- le principali tabelle operative ricevono `tenant_id`, backfill dai `tenant_sellers`, trigger automatico e policy RLS;
- `ENABLE + FORCE ROW LEVEL SECURITY` protegge anche quando il ruolo applicativo è proprietario delle tabelle;
- startup/migrazioni usano un bypass esplicito e limitato (`platform_database_scope`);
- i worker leggono il `tenant_id` del job, verificano che il Seller appartenga a quel tenant e solo dopo eseguono la business logic;
- un job senza tenant non viene più accodato.

## Compatibilità
SQLite resta disponibile per sviluppo/test e non applica RLS. Il layer RLS viene attivato solo con PostgreSQL.

## Cataloghi condivisi
`suppliers`, `price_lists` e le tabelle catalogo condivisibili/globali non vengono forzate dentro una policy proprietaria in v316: il loro modello di condivisione cross-tenant sarà trattato separatamente per non rompere la visibilità `shared/global`.

## Sicurezza
La protezione diventa quindi a due livelli:
1. API: permessi + tenant attivo + Seller assegnati;
2. PostgreSQL: policy RLS sul tenant operativo.
