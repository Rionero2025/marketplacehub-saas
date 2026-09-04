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
