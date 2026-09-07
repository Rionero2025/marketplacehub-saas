# Changelog

## 2026-09-07 — B20.2/B21.2: selezione ordini, riepilogo e CSV

- filtri per stato/paese/valuta, corriere, tracking, commissione e venduto EUR;
- selezione SQL per sessione, Seller, account, ambiente e firma del filtro;
- checkbox stabili tra pagine, ripristino del filtro precedente e azioni su tutto il blocco;
- totali selezionati secondo l'originale, cancellazioni e dati incompleti espliciti;
- CSV selezionati/filtrati a blocchi, UTF-8 BOM, dati pubblici e protezione delle celle testuali;
- QA API con 81 righe sintetiche, 99 test frontend e typecheck passati;
- sei criteri di selezione verificati: 116/2.011 = 5,77%; protocollo API documentato;
- verifiche finali e pubblicazione B20.2 in attesa; CSV completo di pagamenti/ticket ancora pendente;
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
