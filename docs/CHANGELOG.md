# Changelog audit
## 2026-09-04
- Congelate baseline SaaS 972e8f8, legacy 4c3cda5 e installazione locale v271.
- Inventariati 28 moduli/pagine originali, 51 endpoint API e 118 nomi tabella SQL.
- Confrontati simboli, controlli Streamlit e differenze dei file.
- Mappati requisiti Master Spec e gap design system, billing, UX e parità.
- Eseguita suite tests_core: 76 passati, 1 fallimento statico preesistente.
- Generata roadmap delle prime cinque PR.
- Nessun file applicativo modificato; nessuna migrazione né modifica Render eseguita durante l'audit.


## Aggiornamento porting — 4 settembre 2026

PR #1 integrata: CI, test onboarding persistente e controlli OpenAPI. Ramo SKU: helper originali ripristinati con isolamento riga, nessuna modifica ai dati live; suite locale 90 test superati.


### Blocco 03 — verifica autorizzazioni

Blocco 02 integrato (90 test). Blocco 03 in verifica: ruolo di scrittura, ereditarietà Agency, scope esplicito per billing/piani/tenant e restrizione seller legacy; suite PostgreSQL isolata aggiunta alla CI.


### Blocco 04 — sincronizzazioni duplicate

Blocco 03 integrato. Blocco 04: deduplica transazionale degli ordini e test concorrenza SQLite/PostgreSQL; nessuna migrazione dati.


### Blocco 05 — cataloghi atomici

Blocco 04 integrato. Blocco 05: preparazione cataloghi su staging temporaneo, switch atomico, protezione da cambio sorgente e fallback CSV parziale; test fault injection e concorrenza.


### Blocco 06 — recupero job interrotti

Blocco 05 integrato. Blocco 06: heartbeat durante handler silenziosi, riaccodamento conservativo, limite tentativi, errori con verifica richiesta e protezione dai vecchi claim. Test su recupero concorrente, scope del worker, heartbeat e fallimento handler.


### Blocco 07 — integrità e storage condiviso

Blocco 06 integrato (153 test CI). Blocco 07 parziale: cache integre, scritture concorrenti atomiche con retry Windows limitato, versioni immutabili e prova ripristino. Nessuna attivazione S3 senza configurazione disponibile.


### Hotfix — elenco cataloghi HTTP 500

Hotfix cataloghi: ordinamento nella query esterna dopo DISTINCT; aggiunti test endpoint sui due database. PR #7 integrata, 166 test CI, API e worker Render verificati live su aeb653f.
