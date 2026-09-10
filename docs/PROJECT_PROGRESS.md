# Avanzamento del progetto

## Formula

`percentuale = criteri con stato verified / criteri nel perimetro attivo × 100`

Il denominatore è generato e controllabile in `docs/reference/project-acceptance-index.json`:

- 703 regressioni originali;
- 391 interazioni originali;
- 923 requisiti del Master Spec.

Questa percentuale misura la copertura verificata del prodotto finale. Non è una stima delle ore residue. Un elemento documentato ma non implementato resta `pending`.

La baseline originale resta di 2.017 criteri. D-013 esclude 6 criteri esclusivamente relativi
alla ripartizione degli utili: il perimetro attivo è **2.011**. I criteri misti mantengono le
verifiche su costi, margini, export e dashboard. Gli override espliciti sono riproducibili in
`docs/progress/scope-overrides.json`; il generatore li applica senza alterare gli originali.

## Stato corrente

| Blocco | Stato | Criteri verificati nel blocco | Progetto totale |
|---|---|---:|---:|
| B01 — Audit e inventario | completato | 26 | **1,29%** |
| B02 — Fondazione tecnica | completato | 8 | **1,69%** |
| B03 — Autenticazione e accessi | completato | 15 | **2,43%** |
| B04 — Organizzazioni e negozio attivo | completato | 21 | **3,47%** |
| B10.1 — Anagrafica e account Kaufland | completato; B10 resta parziale | 9 | **3,93%** |
| B10.2 — Collega marketplace, Seller Enterprise | completato nel perimetro Kaufland/Worten; altri connettori pendenti | 8 | **4,33%** |
| B20.1 / B21.1 — Archivio ordini Kaufland/Worten e navigazione Seller | collaudato e pubblicato nello staging; moduli Ordini ancora parziali | 23 | **5,47%** |
| B20.2 / B21.2 — Filtri, selezione, riepilogo e CSV ordini | collaudato e pubblicato nello staging; moduli Ordini ancora parziali | 6 | **5,77%** |
| B20.3 — Importazione e correzione tracking | collaudato e pubblicato nello staging; invio ai marketplace escluso | 10 | **6,27%** |
| B20.4 — Scadenziario pagamenti e ritardi ticket | collaudato e pubblicato nello staging per Kaufland; settlement escluso | 21 | **7,31%** |

Calcolo corrente: `147 / 2.011 = 7,3098%`, mostrato con due decimali. I totali B01–B04 nella
tabella sono quelli storici al rilascio. Il precedente passaggio da 3,97% a 3,93% derivava
dal nuovo perimetro: dei 6 criteri esclusi, uno era verificato e cinque pendenti. La rimozione
non viene conteggiata come nuova funzione completata.

B20.4 aggiunge 21 criteri: eventi distinti di consegna, spedizione e rilascio, previsioni
Kaufland +14/+21, countdown UTC, priorità della data effettiva, ritardi dei ticket senza
doppio conteggio, riparazione delle righe precedenti, filtro pagamento e seconda selezione
persistente con riepilogo disponibile/in attesa. Marketplace privi di adattatore verificato
non ricevono regole Kaufland. Verificati 401 test Python, 136 test frontend, typecheck, build
17/17, migrazione 0009 e protezioni live. Web, API e worker sono `Live` sul commit
`ca7eb3651f19118ae43eda21b66032bbd3dd8ec4`; health/readiness e PostgreSQL/Redis sono attivi.
Il collaudo automatico non ha trasmesso credenziali né modificato dati reali di Rionero.
Vedere `docs/blocks/B20_4_PAYMENTS_RELEASE.md`.

B20.3 aggiunge 10 criteri: anteprima CSV/XLS/XLSX e mappatura correggibile, importazione
parziale con conteggi espliciti, modifica manuale della singola unità e conservazione dei
tracking locali durante sincronizzazioni API prive di nuovi valori. Il flusso resta nello
scope di organizzazione, Seller, account ed ambiente, con permesso Logistica, audit e limiti
di upload. Verificati 353 test Python e 120 test frontend al congelamento; dopo l'hotfix
auth la suite finale è **124/124**, con typecheck, build e 17/17 pagine completati. Web,
API e worker sono Live sul commit `160fa00`; implementazione tracking `e36a646`, migrazione
`20260908_0008`, health/readiness e quattro route protette risultano pubblicati. Nessun file
tracking reale di Rionero è stato importato nel collaudo. Vedere
`docs/blocks/B20_3_TRACKING_RELEASE.md`.

B20.2/B21.2 aggiunge soltanto i sei criteri `LEGACY-TEST-0435`–`0440`: selezione per
firma del filtro, ID stabili e persistenza tra pagine. Gli input malformati che l'helper
Streamlit ignorava sono rifiutati dall'API con 422 senza alterare lo stato. Sono operativi
filtri avanzati, totali economici del blocco selezionato e CSV selezionati/filtrati;
nessun criterio generico di tabella completa, contabilità o Excel viene chiuso per questo.
Verificati 300 test Python, inclusi 16 focalizzati sul blocco, 99 test frontend al
rilascio, typecheck, build e QA API indipendente con 81 righe sintetiche: paginazione,
selezioni, filtri, totali, CSV e isolamento. La suite frontend è poi salita a 102/102
per la correzione auth `95fe6b5`, senza nuovi criteri B20.2. Il blocco è online con
implementazione `34a7932` e correzione finale del pannello `ccb19db`; Web, API, worker,
migrazione `20260907_0007` e readiness sono attestati nella scheda di rilascio.

B20.1/B21.1 aggiunge 23 criteri di parità verificati: importazione multicanale in background,
paginazione e stati Kaufland, archivio isolato e aggiornamento senza duplicati, dati prodotto,
SKU/costo, commissioni, quantità Worten, cambi, tracking letto dalle API, ricerca e scelta
account/ambiente. Verificati 266 test Python (85 Ordini), 80 test frontend e browser desktop
e responsive; build produzione completata. Il rilascio staging è verificato per web,
API e worker sul commit `4fd69d622d19c6e6f1ed48f5d0cff9168cac3466`, con migrazione
Ordini, readiness e protezione delle rotte confermate nella scheda di rilascio.
La navigazione a macroaree/sottosezioni richiesta dall'utente non incrementa il ledger.
Vedere `docs/blocks/B20_1_ORDERS_RELEASE.md`, contratto sorgente B20.1 e D-016.

La copertura locale verificata è distinta dal rilascio online e dall'importazione di dati
reali: sono attestati i rilasci staging B20.1–B20.4, non un'importazione automatica di
fixture nell'account Rionero. Listini e fallback costi, settlement/booking report, invio
tracking ai marketplace, selezioni contabili complete e connettori ulteriori restano
pendenti. Il modulo Ordini e tutte le colonne dell'export originale non sono dichiarati
completi.

B10.2 aggiunge 8 criteri effettivamente verificati: form/verifica Worten, account salvato,
parser storefront originale, test connessione e metadata veri. La griglia di 28 marketplace
non equivale a 28 connettori implementati: Kaufland/Worten sono disponibili per collegamento,
26 sono esplicitamente da sviluppare. In B10.2 nessuna sincronizzazione ordini era conteggiata.
Verifiche: 181 test Python, 52 test frontend, typecheck, build e browser desktop/mobile.
Vedere `docs/blocks/B10_2_MARKETPLACE_CONNECTIONS.md`, D-014 e D-015. Le nuove funzioni si
concentrano sul Seller Enterprise senza limitazioni commerciali; Agency e Platform rinviate.

B10.1 è stato anticipato su richiesta dell'utente per dare operatività al Seller. Sito/piani,
billing e onboarding B05–B07 restano pendenti. Il blocco trasferisce configurazione e
salvataggio credenziali Kaufland; connessione API e sincronizzazione ordini non sono incluse.
I 9 ID attivi sono elencati nel ledger con evidenza `docs/blocks/B10_1_SELLER_SETTINGS.md`.

Correzione D-013: rimossi campi, DTO, validazioni, normalizzazione e letture/scritture delle
quote. Salvataggio anagrafica indipendente dalle percentuali; dati importati conservati.
Verifiche della correzione: 29 test API/core, 36 test frontend, typecheck e build produzione.

Rifinitura grafica B04 del 7 settembre 2026: dashboard ispirata a Base.com, con navigazione
compatta, pannelli e tabella autorizzazioni responsive. Nessun criterio operativo aggiuntivo:
la copertura funzionale resta **70/2.017 (3,47%)**. Vedere decisione D-011.

Correzione B03 del 7 settembre 2026: trasferimento delle identità precedenti e compatibilità con
le password PBKDF2, mancanti nel primo rilascio. Il controllo iniziale di rifiuto credenziali non
verificava l'accesso di un account esistente. La correzione non incrementa la copertura del prodotto;
il totale resta 49/2.017. Le funzioni operative del Seller restano da ricostruire.

I primi 26 criteri soddisfatti sono gli output della Fase 0 e i documenti permanenti. B02 aggiunge
la fondazione eseguibile. B03 aggiunge autenticazione e sessioni. B04 aggiunge organizzazioni,
membership, autorizzazione backend e selezione del negozio con i dati precedenti. Gestione completa
di utenti/Seller, abbonamenti e altri moduli operativi Streamlit restano pendenti.
