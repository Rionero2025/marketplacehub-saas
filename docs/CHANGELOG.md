# Changelog

## 2026-09-07 — Blocco 4: organizzazioni e negozio attivo

- importate organizzazioni, anagrafiche Seller e assegnazioni precedenti con UUID stabili;
- introdotti ruoli e permessi, gerarchia Agency/Seller e scope verificato a ogni richiesta;
- mantenute precedenza dei ruoli diretti, restrizioni personali e revoche;
- mostrati dati reali nei tre portali con scelta del negozio persistente per sessione;
- reso transazionale il bootstrap Platform;
- aggiunti stati recuperabili per indisponibilità temporanea della verifica sessione;
- 21 criteri aggiunti; copertura verificata totale 70/2.017, pari al 3,47%.

## 2026-09-07 — Blocco 3: autenticazione e accessi

- creati accessi visibili Seller e Agenzia e percorso interno Platform;
- protette le tre destinazioni tramite verifica server-side del realm;
- introdotti utenti, realm e sessioni persistenti con migrazione reversibile;
- aggiunti Argon2id, token opachi, revoca, scadenza e rate limit Redis;
- aggiunti cookie protetti, CORS restrittivo, validazione API e BFF Next.js;
- aggiunto comando interattivo per creare il primo Platform Admin;
- superati test, lint, typecheck, build e prova migrazione.

## 2026-09-07 — Blocco 2: fondazione tecnica

- creati workspace Next.js per sito pubblico e applicazione SaaS;
- create API FastAPI, readiness PostgreSQL/Redis e request ID;
- creato worker RQ separato;
- aggiunti core condiviso, pool PostgreSQL e configurazione tipizzata;
- introdotte migrazioni Alembic reversibili;
- aggiunti Compose locale e Blueprint Render staging validato;
- bloccate dipendenze frontend e Python;
- superati test, lint, typecheck e build di produzione.

## 2026-09-07 — Blocco 1: audit e inventario

- congelato e referenziato il SaaS precedente;
- creato il ramo pulito di ricostruzione;
- fissato il contratto di parità Streamlit;
- analizzata in sola lettura la versione locale 271;
- generati manifest sorgente e indice dei criteri;
- documentati architettura, moduli, database, integrazioni, rischi e decisioni;
- definito il metodo riproducibile della percentuale complessiva.
