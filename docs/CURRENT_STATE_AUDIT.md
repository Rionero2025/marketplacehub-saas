# Audit dello stato iniziale

Data: 7 settembre 2026.

## Perimetro e riferimenti

La ricostruzione parte da un ramo vuoto e non modifica il programma originale.

- SaaS precedente congelato: commit `93cab535e89f36d8149c5298f70463d2c297c7ac`.
- Archivio del SaaS precedente: `archive/pre-rebuild-2026-09-07`.
- Ramo della ricostruzione: `rebuild/streamlit-parity-v2`.
- Streamlit GitHub letto in sola lettura: `Rionero2025/marketplacehub-1`, commit `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`.
- Streamlit locale letto in sola lettura: versione `271`.
- Master Spec: SHA-256 `2A3275DC4066B40C1BFAC79AAFCC78AB1A2FC6137D97152B21DA18F3C98495BE`.

La versione locale 271 è la sorgente funzionale più recente. Il clone GitHub serve per verificare la versione pubblicata e il deployment cloud. Le differenze non vengono risolte scegliendo automaticamente la variante più semplice.

## Dimensione verificata del programma originale

L'analisi AST riproducibile della versione 271 rileva:

- 215 file Python fra applicazione, pagine, servizi e test;
- 88.510 righe Python;
- 27 file pagina oltre all'entrypoint;
- 1.136 funzioni pubbliche e 122 classi pubbliche;
- 391 interazioni Streamlit da riprodurre;
- 703 casi di regressione esistenti;
- 98 tabelle create dal codice applicativo.

Il dettaglio con file, righe, hash, funzioni, import, interazioni e test è in `docs/reference/streamlit-v271-manifest.json`. La generazione è ripetibile con `tools/extract_reference_manifest.py`.

## Stack e ingresso reale

Il programma originale è un monolite Streamlit/Python. Le pagine invocano direttamente i servizi e molte operazioni di rete o di calcolo durante l'esecuzione della pagina. Il livello dati supporta SQLite locale e PostgreSQL attraverso un adattatore. La release cloud GitHub usa Python 3.12, Docker, PostgreSQL 17 e un singolo Web Service Render.

L'entrypoint locale v271 è `app.py`; la navigazione porta alle pagine in `pages/`. La release cloud esegue prima `tools/online_preflight.py`, poi `streamlit run app.py` sulla porta Render.

## Dati e stato

Il database è costruito incrementalmente dai servizi con `CREATE TABLE IF NOT EXISTS` e `ALTER TABLE`. Seller, account marketplace e molte cache operative hanno già uno scope, ma non esistono ancora le entità SaaS Organization, Membership, Subscription, Entitlement e Audit Session richieste dal Master Spec.

Le credenziali applicative sono cifrate con Fernet a partire da `MARKETPLACE_HUB_MASTER_KEY`. Lo stato dell'interfaccia usa anche `st.session_state`. File importati, esportazioni e cache sono gestiti da più moduli e devono essere separati fra dati durevoli, artefatti e cache rigenerabile nella nuova architettura.

## Variabili d'ambiente rilevate

La configurazione dati usa `MARKETPLACE_HUB_DB_ENGINE`, `MARKETPLACE_HUB_PG_HOST`, `MARKETPLACE_HUB_PG_PORT`, `MARKETPLACE_HUB_PG_DATABASE`, `MARKETPLACE_HUB_PG_USER`, `MARKETPLACE_HUB_PG_PASSWORD`, `MARKETPLACE_HUB_PG_SSLMODE`, `MARKETPLACE_HUB_PG_POOL_MIN`, `MARKETPLACE_HUB_PG_POOL_MAX` e `MARKETPLACE_HUB_PG_CONNECT_TIMEOUT`. Sono inoltre usate `MARKETPLACE_HUB_MASTER_KEY` e, per le funzioni IA, `OPENAI_API_KEY`. La release cloud contiene anche la configurazione di autenticazione amministrativa Streamlit.

Nessun valore segreto è stato copiato nell'audit.

## Deployment Render esistente

`render.yaml` descrive un servizio Web Docker in regione Francoforte, piano free, con health check Streamlit e un PostgreSQL 17. Il container installa anche Tesseract. Non sono presenti nella release originale processi worker separati, coda, scheduler o storage oggetti gestito come componenti autonomi.

## Test esistenti

I 703 test coprono soprattutto contabilità, catalog intelligence, ordini Kaufland, Buy Box, Packlink, Cecotec, Innpro, tracking, supporto, dashboard, migrazione dati e compatibilità PostgreSQL. Sono test Python; non costituiscono una suite browser end-to-end del prodotto SaaS.

Aree prive di una verifica completa end-to-end: autenticazione Seller/Agency/Platform, isolamento tenant, abbonamenti e webhook, onboarding, autorizzazioni backend, sito pubblico, flussi reali multiutente, resilienza del worker, restore di backup e confronto visivo/operativo completo fra Streamlit e SaaS.

## Checklist obbligatoria della Fase 0

| Requisito | Esito | Evidenza |
|---|---|---|
| Mappa repository, stack ed entrypoint | completato | questo documento e `ARCHITECTURE.md` |
| Database reale | completato | `DATABASE.md` |
| Moduli | completato | `MODULES.md` |
| Integrazioni | completato | `INTEGRATIONS.md` |
| Variabili d'ambiente | completato | questo documento |
| Deployment Render | completato | questo documento e `ARCHITECTURE.md` |
| Rischi e colli di bottiglia | completato | `KNOWN_ISSUES.md` |
| Duplicazioni | completato | `KNOWN_ISSUES.md` |
| Dipendenze | completato | `KNOWN_ISSUES.md` |
| Test esistenti e lacune | completato | questo documento e manifest |
| Contratto di parità | completato | `STREAMLIT_PARITY_CONTRACT.md` |
| Inventario e misura di avanzamento | completato | `FUNCTIONAL_INVENTORY.md` e `PROJECT_PROGRESS.md` |

## Esito

Il motore originale è ampio e già contiene logiche operative mature. La nuova applicazione non deve ricostruirle per somiglianza: ogni blocco deve portare nel core SaaS gli stessi input, trasformazioni, persistenza, output e casi di errore. Il ramo nuovo non contiene ancora runtime applicativo; questo evita che componenti del SaaS precedente vengano erroneamente considerati equivalenti.
