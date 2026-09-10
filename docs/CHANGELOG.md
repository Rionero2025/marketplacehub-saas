# Changelog

## 2026-09-10 — B20.4: scadenziario pagamenti e ritardi ticket

- trasferite le regole Kaufland per rilascio effettivo, consegna +14 giorni e
  spedizione +21 giorni, senza applicarle agli altri marketplace;
- aggiunti countdown UTC, priorità della data effettiva e rinvio per ticket aperti o
  chiusi con unione degli intervalli sovrapposti;
- aggiunti filtro pagamento, campi pagamento/ticket in lista, dettaglio e CSV, seconda
  selezione persistente e riepilogo disponibile/in attesa;
- aggiunti snapshot ticket isolati per Seller/account/ambiente e migrazione 0009 con
  backfill, compatibilità progressiva e downgrade verificato;
- separati visivamente incassi marketplace e margine calcolabile; copertura del margine,
  righe escluse e perdite sono ora espliciti;
- superati 401/401 test Python e 136/136 test frontend, typecheck e build con 17/17
  pagine; migrazione e route protette verificate nello staging;
- aggiunti 21 criteri verificati: 147/2.011 = 7,31%; `LEGACY-TEST-0375` resta pendente;
- pubblicato con implementazione `e83bc32` e correzione finale `ca7eb36`: Web, API e
  worker Live sullo stesso commit, health/readiness e PostgreSQL/Redis attivi;
- nessuna credenziale reale trasmessa e nessuna modifica automatica ai dati di Rionero
  durante il collaudo.

## 2026-09-10 — B20.3: importazione e correzione tracking

- trasferiti anteprima e importazione Kaufland da CSV, XLSX e XLS, con mappatura
  automatica e correggibile dei campi;
- aggiunti esiti parziali per unità aggiornate, righe non abbinate e righe non valide;
- aggiunta correzione manuale per singola unità, con corriere e tracking precompilati e
  conservazione dei valori omessi;
- mantenuti i tracking locali quando una sincronizzazione API successiva restituisce
  valori vuoti; i nuovi valori API non vuoti restano autorevoli;
- applicati scope Seller/account/ambiente, permesso Logistica, audit, admission control,
  timeout e limiti di file, righe, colonne, celle e contenuto;
- superati 353/353 test Python e 120/120 test frontend al congelamento del blocco;
  sul commit finale, dopo l'hotfix auth, 124/124 test frontend, typecheck e build con
  17/17 pagine;
- aggiunti 10 criteri verificati: 126/2.011 = 6,27%; nessun criterio aggiunto per
  l'hotfix auth;
- pubblicato nello staging con implementazione `e36a646` e commit finale `160fa00`:
  Web, API e worker Live, migrazione 0008 applicata, health/readiness e quattro route
  tracking protette verificate;
- il collaudo non ha importato file tracking reali di Rionero e non invia tracking ai
  marketplace.

## 2026-09-10 — B03: recupero automatico dopo il cold start

- avviato il risveglio anonimo dell'API all'apertura del login e ripetuta la readiness
  prima di leggere o inviare le credenziali;
- eliminato l'errore rimasto a schermo dopo il recupero del servizio e mostrato lo stato
  pronto senza eseguire automaticamente il login;
- pubblicato l'hotfix `160fa00` su Web, API e worker; nessun incremento della percentuale.

## 2026-09-07 — B20.2/B21.2: selezione ordini, riepilogo e CSV

- filtri per stato/paese/valuta, corriere, tracking, commissione e venduto EUR;
- selezione SQL per sessione, Seller, account, ambiente e firma del filtro;
- checkbox stabili tra pagine, ripristino del filtro precedente e azioni su tutto il blocco;
- totali selezionati secondo l'originale, cancellazioni e dati incompleti espliciti;
- CSV selezionati/filtrati a blocchi, UTF-8 BOM, dati pubblici e protezione delle celle testuali;
- QA API con 81 righe sintetiche, 300 test Python, 99 test frontend al rilascio,
  typecheck e build passati; suite frontend poi salita a 102/102 con l'hotfix auth;
- sei criteri di selezione verificati: 116/2.011 = 5,77%; protocollo API documentato;
- pubblicato nello staging con implementazione `34a7932` e correzione finale del pannello
  `ccb19db`; Web, API, worker, migrazione 0007 e readiness verificati;
- CSV completo di pagamenti/ticket ancora pendente;
- corretta la documentazione B20.1 con i tre deploy Live e le verifiche online già attestate.

## 2026-09-07 — B20.1/B21.1: archivio ordini e navigazione Seller

- aggiunta macroarea Ordini con importazione Kaufland/Worten dall'account collegato al Seller;
- coda e worker separati, stato durevole, controlli di scope/permessi e archivio senza duplicati;
- trasferiti SKU composto, nome/EAN, quantità, costi, vendita, commissioni, netto e utile;
- conservate differenze monetarie tra Kaufland e Worten, provenienza e valori mancanti espliciti;
- disponibili ricerca, filtri data/stato/paese, dettagli e ambienti Kaufland Live/Playground;
- barra scura con macroaree e menu chiaro con sottosezioni dedicate, secondo D-016;
- verificati 266 test Python (85 Ordini), 80 test frontend, build e browser desktop/responsive;
- 23 criteri verificati aggiunti: 110/2.011 = 5,47%; nessun incremento per il solo restyling;
- pubblicazione staging verificata sul commit `4fd69d6`; importazione reale non attestata;
- restano pendenti parità completa dei listini, pagamenti/ticket, tracking manuale e CSV.

## 2026-09-07 — B03: accesso durante il risveglio del backend

- readiness prima dell'unico invio delle credenziali e messaggi specifici per l'indisponibilità;
- timeout, annullamento e controllo della sessione restituita dal backend;
- nessuna modifica delle password, del piano Render o della percentuale del progetto;
- diagnosi e verifiche in `docs/blocks/B03_LOGIN_COLD_START.md`.

## 2026-09-07 — B10.2: Collega marketplace nel Seller Enterprise

- nuova sezione dedicata con griglia grafica di 28 marketplace, ricerca e filtri;
- verifica e collegamento API per Kaufland/Worten, altri connettori dichiarati da sviluppare;
- credenziali cifrate, riverifica account importati, metadata reali e stato/errori specifici;
- host fissi, timeout, limiti risposta, isolamento Seller e protezione delle verifiche concorrenti;
- separata l'anagrafica dalla gestione multicanale; nessuna ripartizione utili o limite di piano;
- 181 test Python, 52 test frontend, typecheck, build e collaudo browser desktop/mobile;
- 8 criteri verificati aggiunti: 87/2.011 = 4,33%.

## 2026-09-07 — D-013: esclusa la ripartizione degli utili

- rimosse percentuali e divisione nostro/partner dal form, DTO e logica attiva del SaaS;
- anagrafica salvata con nome, ragione sociale ed email, senza vincolo somma 100%;
- conservati dati storici e migrazioni, senza letture o aggiornamenti operativi delle quote;
- esclusione applicata anche ai futuri blocchi Agency, dashboard e contabilità;
- mantenuti nel perimetro costi, ricavi, commissioni, margine e utile del singolo Seller;
- baseline immutata; 6 criteri esclusi, copertura attiva 79/2.011 = 3,93%;
- verificati 29 test API/core, 36 test frontend, typecheck e build produzione.

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
