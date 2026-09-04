# Avanzamento del porting SaaS

Baseline del piano: audit del 4 settembre 2026. Requisiti: Master Spec allegato,
inventario delle 28 aree Streamlit e differenze dell'installazione locale v271.

## Come si calcola la percentuale

40 blocchi di consegna, ciascuno pari a 2,5 punti: `blocchi completati / 40 × 100`.
È una metrica di milestone verificate, non una stima delle ore, delle righe di
codice o del valore del prodotto. I blocchi hanno complessità diversa. Il codice
già esistente è una base da verificare e riutilizzare; la sua presenza non chiude
automaticamente un blocco. L'audit è la baseline e non viene contato come porting.

Completato significa: ambito verificato, test pertinenti superati, commit integrato
su main. Per i flussi utente servono anche verifica UI e prova completa sullo
staging; pubblicazione GitHub e stato del deploy Render sono distinti. Un blocco
parziale vale zero fino alla chiusura. Le suddivisioni interne in PR non cambiano
il denominatore. Nuovi requisiti cambiano la baseline solo con una revisione
esplicita e documentata, mai per far salire la percentuale.

**Totale attuale: 6/40 = 15%.**

| ID | Blocco e risultato richiesto | Stato |
|---|---|---|
| 01 | CI esegue i test; registrazione verifica persistenza e collegamenti | DONE — PR #1, CI 33874550242 superata |
| 02 | Recupero SKU/costi originale e override contabili preservati | DONE — PR #2, CI 33874967176 superata |
| 03 | Ruoli membership e accessi tenant coerenti, prove PostgreSQL/RLS | DONE — PR #3, 108 test CI superati |
| 04 | Deduplica concorrente dei job ordini per tenant/account/parametri | DONE — PR #4, CI 33876276914 superata |
| 05 | Materializzazione cataloghi atomica e ultima versione valida | DONE — PR #5, 129 test CI superati |
| 06 | Recupero job orfani, retry controllato e stato attendibile | DONE — PR #6, 153 test CI superati |
| 07 | Storage condiviso persistente API/worker e verifica restore | IN PROGRESS — PR #7 integrata, 166 test CI; bucket live e restore da verificare |
| 08 | Protezioni autenticazione, sessioni e recupero account previste | TODO |
| 09 | Sync ordini incrementale/bulk, misure prima/dopo e parità risultati | TODO |
| 10 | Dashboard con richieste aggregate e invalidazione scope verificata | IN PROGRESS — implementazione Seller in verifica, prova dati staging e parità restante aperte |
| 11 | Cache, import grandi e memoria misurati senza perdere dati/funzioni | TODO |
| 12 | Design system condiviso React/TS/Tailwind/shadcn e stati UI | TODO |
| 13 | Gestione Seller, utenti, team, account marketplace e logistica | TODO |
| 14 | Workspace Agency, clienti, assegnazioni e dashboard dedicata | IN PROGRESS — prima struttura, completamento ancora aperto |
| 15 | Platform admin, gestione tenant, supporto e dashboard dedicata | IN PROGRESS — prima struttura, completamento ancora aperto |
| 16 | Fornitori, feed e listini: configurazione, CRUD, upload e parser originali | TODO |
| 17 | Lavorazione cataloghi: query, filtri, selezioni, trasformazioni e viste | TODO |
| 18 | Ordini Kaufland: parità completa filtri, colonne, sync, import/export | TODO |
| 19 | Ordini Worten/Mirakl: parità del flusso completo e stati | TODO |
| 20 | Contabilità operativa, listino corretto, edit persistenti, margini, quote, export | IN PROGRESS — editor/listini/Excel in verifica; import, PDF, storico e prova staging aperti |
| 21 | Pagamenti marketplace/settlement, previsioni, riconciliazioni e rettifiche | TODO |
| 22 | Buy Box Kaufland/Worten, controllo, filtri margine e azioni prezzo | TODO |
| 23 | Pubblicazione e cancellazione offerte sui marketplace esistenti | TODO |
| 24 | Configurazione provider/profili AI e credenziali per scope | TODO |
| 25 | Creazione prodotti: taxonomy, AI, localizzazione, validazione e invio | TODO |
| 26 | Packlink: tariffe, scelta migliore, massivo, correzioni e fallback CSV | TODO |
| 27 | Tracking, documenti, archivi e aggiornamento marketplace | TODO |
| 28 | Ordini fornitori Cecotec/Innpro, template e storico download/dedup | TODO |
| 29 | Ticket/messaggi marketplace, lettura, risposte, filtri e ordine associato | TODO |
| 30 | Dashboard Seller, Top 10, vendite, alert e card operative cliccabili | IN PROGRESS — implementazione Seller in verifica, prova dati staging e parità restante aperte |
| 31 | Storico operativo e Attività con progressi/errori comprensibili | TODO |
| 32 | Backup/trasferimento dati e amministrazione database con permessi dedicati | TODO |
| 33 | Stripe reale: Checkout, Billing, Portal, webhook e ciclo abbonamento | TODO |
| 34 | Fatture in Cloud secondo Master Spec e casi verificati | TODO |
| 35 | Marketing pubblico conforme al design system e collegamenti all'app | IN PROGRESS — prima struttura, completamento ancora aperto |
| 36 | Onboarding SaaS completo, piani, trial, limiti ed entitlement nel prodotto | IN PROGRESS — prima struttura, completamento ancora aperto |
| 37 | Contratti adapter comuni; roadmap canali futuri senza dichiararli operativi | TODO |
| 38 | Log/correlation ID, monitoraggio, runbook e documentazione aggiornata | TODO |
| 39 | Verifica trasversale: isolamento, regressioni legacy, carico e sicurezza | TODO |
| 40 | Accettazione finale: ogni requisito tracciato, flussi staging, deploy/rollback | TODO |

## Regole di parità

Ogni blocco funzionale deve coprire tutti i controlli pertinenti dell'inventario e
del Master Spec, non soltanto il suo titolo. Le funzioni tecniche legacy restano
disponibili con autorizzazioni appropriate. Non creare dati commerciali fittizi
per riempire dashboard. Nessun canale futuro è implicitamente operativo: la
sezione 44 del documento prescrive una roadmap, non tutte le integrazioni insieme.

## Registro

- 2026-09-04: blocco 01 integrato su main con PR #1, commit
  `ebb4d6ea3b644293bdd0777d9dfb252413a56757`. 77 test locali; CI GitHub riuscita
  dopo conversione dei controlli route allo schema OpenAPI pubblico.
- 2026-09-04: blocco 02 in verifica; 11 casi inizialmente falliti ora superati
  localmente. Non ancora contato nel totale.

- Blocco 02 integrato con PR #2, commit de25a150acd2d42713dd404d38e527c9a557ed52; 90 test e CI superati. Totale 5%.
- Blocco 03: verifiche in corso; non ancora conteggiato.

- Blocco 03 integrato con PR #3 (eeae695), 108 test CI superati comprese due prove PostgreSQL. Totale 7,5%.
- Blocco 04 in verifica: deduplica atomica ordini, 113 test locali superati e tre prove PostgreSQL affidate alla CI.

- Blocco 04 integrato con PR #4 (8d82773), CI con concorrenza PostgreSQL riuscita. Totale 10%.
- Blocco 05: staging privato per connessione e pubblicazione atomica, verifiche in corso.

- Blocco 05 integrato con PR #5 (5b72c7f), 129 test GitHub CI superati. Totale 12,5%.
- Blocco 06: heartbeat indipendente, recupero controllato e protezione del claim; 132 test locali superati e 21 PostgreSQL demandati alla CI.

- Blocco 06 integrato con PR #6 (12c447e), 153 test GitHub CI superati. Totale 15%.
- Blocco 07 parziale: integrità cache, versioni immutabili, prova restore e runbook. Nessun incremento prima di configurazione e prova API/worker live.

- Hotfix elenco cataloghi PostgreSQL in verifica. Totale invariato: 15%.
- Chiarimento utente: sito pubblico con piani Free, Bronze, Silver, Gold, Platinum. Altre due macroaree da precisare; vedere USER_REQUIREMENTS_UPDATES.md.

- PR #8 integrata: 170 test CI; hotfix cataloghi API live su 3238da7.
- Nuovo requisito precisato: sei piani e tre macroaree app Admin/Seller/Agency; sito pubblico separato. Prima struttura in verifica, nessun incremento percentuale.

- PR #9 integrata e verificata Live su a1e8ed1 (frontend, API e worker), 176 test CI.
- Accessi distinti Seller/Agenzia/Admin in verifica; avvio pannello Seller. Totale invariato 6/40 = 15%.

- PR #10 integrata e frontend/API aggiornati (e8f82ab), 187 test CI.
- Porting dashboard Seller in verifica: stesso motore economico, periodo e Top 10. Nessun incremento prima della prova completa prevista.

- PR #11 dashboard pubblicata, 213 test CI.
- Contabilità operativa Seller in verifica; totale invariato 6/40 = 15%.


### Verifica del percorso dati contabile — 4 settembre 2026

Consegna del percorso dati: proiezione Ordini corretta, riga contabile completa e prove reali di persistenza/API con dati sintetici. I blocchi funzionali restano parziali fino a parità del flusso e collaudo staging autenticato. Totale invariato: **6/40 = 15%**.


### Selezione e modifica multipla contabilità — 4 settembre 2026

Selezione e modifica multipla contabile implementate; parità complessiva e collaudo autenticato ancora aperti. Totale milestone invariato: **6/40 = 15%**.
