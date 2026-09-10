# B20.3 — Importazione e correzione tracking nell'archivio ordini

Data: 8 settembre 2026. Perimetro: pannello Seller Enterprise, senza limiti
commerciali di pacchetto. Il programma Streamlit originale resta in sola lettura.
Contratto sorgente: `B20_3_TRACKING_SOURCE_CONTRACT.md`.

Stato del documento: **pubblicato e collaudato nello staging**.

## Funzioni trasferite

La macroarea **Ordini** aggiunge due operazioni sull'archivio del marketplace e account
selezionati a monte. Entrambe aggiornano soltanto il dato interno: nessun tracking viene
inviato al marketplace.

- Anteprima server-side di export CSV, XLSX e XLS, con intestazioni e prime 10 righe.
  Il riconoscimento propone ID unità, numero ordine, corriere, tracking e campo combinato;
  il Seller può correggere tutte le associazioni prima di importare.
- Normalizzazione originale delle intestazioni, priorità dei campi separati sul campo
  combinato e separazione del combinato nell'ordine `|`, ` - `, `;`. I tracking separati
  da virgola vengono ripuliti e deduplicati conservando l'ordine.
- Importazione a esito parziale: il riepilogo distingue unità aggiornate, righe non
  abbinate e righe non valide. L'ID unità ha priorità; in sua assenza il numero ordine
  aggiorna tutte le unità corrispondenti nello stesso scope.
- Correzione manuale della specifica unità scelta dall'archivio, con ordine e prodotto
  visibili e corriere/tracking precompilati. Un campo lasciato vuoto conserva il valore
  esistente dell'altro campo.
- Persistenza compatibile con le sincronizzazioni: un successivo valore API vuoto non
  cancella il dato locale; un nuovo valore API non vuoto lo sostituisce, come nel codice
  Streamlit originale.
- Aggiornamento immediatamente visibile nella proiezione ricercabile dell'archivio, senza
  ricreare UUID o selezioni già salvate dal blocco B20.2.

La prima parità verificabile è **Kaufland**, perché le funzioni e i test originali letti
per questo blocco sono Kaufland. L'architettura lega il comando all'account marketplace
selezionato, ma questo documento non rivendica lo stesso formato per altri connettori:
ognuno richiederà un contratto e prove propri.

## Adattamento SaaS, isolamento e audit

Il flusso usa quattro confini API dedicati: capability, anteprima, importazione e
correzione manuale. Il backend ricava identità e scope dalla sessione e verifica
organizzazione/Seller, permesso `LOGISTICS`, negozio attivo, account ed ambiente. Gli
identificativi forniti dal browser non consentono di leggere o aggiornare righe di un
altro Seller, account o ambiente.

Le autorizzazioni vengono ricontrollate immediatamente prima delle scritture e l'account
viene verificato nuovamente nella transazione. Rimane il limite ordinario delle sessioni
già autorizzate: una revoca che avvenga dopo l'ultimo controllo, mentre la richiesta è
già in esecuzione, può lasciare terminare quella singola operazione in volo. Le richieste
successive applicano la revoca. Questo limite residuo non amplia lo scope autorizzato
della richiesta già iniziata.

L'importazione produce eventi di audit con attore, scope, origine `portal_import` o
`manual` e timestamp. La migrazione applicata `20260908_0008_order_tracking.py` aggiunge
la struttura di audit senza eliminare o reinterpretare gli ordini esistenti.

Il file resta in memoria per il tempo necessario a parsing e scrittura e non viene
archiviato. Il server applica admission control prima di leggere il corpo: massimo due
elaborazioni concorrenti globali, una per attore e una per account, coordinate anche
tramite Redis. Sono presenti timeout separati per lettura e chiamata a monte; conflitti
di account e saturazione restituiscono esiti recuperabili senza confermare scritture
incerte.

## Limiti applicati

Il frontend pubblica i limiti di 5 MiB, 10.000 righe dati, 100 colonne, 200.000 celle
logiche incluse le intestazioni e 2.000 caratteri per cella. L'anteprima mostra 10 righe
e un import non può aggiornare più di 10.000 unità ordine. Il contratto sorgente elenca
anche le soglie interne applicate a ZIP, XML, stringhe condivise, stili, workbook e fogli
XLSX.

La lettura XLS/XLSX conserva la semantica originale del primo foglio. Gli altri fogli
non vengono importati; per XLSX sono controllati soltanto nella parte che la libreria
deve leggere per individuarne la dimensione, oltre ai limiti e divieti globali del file.

## Verifiche locali

Le verifiche sono state eseguite sul codice congelato prima della pubblicazione:

- suite Python completa: **353/353 test superati**; suite pertinente a tracking,
  Ordini e migrazione: **109/109**, di cui **20/20** prove mirate del parser e delle
  capability;
- suite frontend: **120/120 test superati** al congelamento del blocco; sul commit finale,
  dopo l'hotfix indipendente dell'accesso, **124/124**; build Next.js 16.3.4, controllo
  TypeScript e generazione di 17/17 pagine statiche completati, con tutte le quattro
  route tracking;
- Ruff superato sui 12 file Python del blocco e `pip check` senza dipendenze rotte;
- `pip-audit` sia sul lock sia sull'ambiente e `pnpm audit` senza vulnerabilità note;
- unica head Alembic `20260908_0008`; roundtrip dedicato 0007→0008→0007 **1/1**
  superato, con audit, chiavi esterne e duplicati legacy verificati;
- `git diff --check` superato e manifest Streamlit rigenerato: **215/215 file**
  invariati, SHA-256 del manifest
  `596CB358FF1329342DB0C7D62034744CD405D782E906AE00C4DAF35FABC82501`.

Il collaudo staging del 10 settembre 2026 ha verificato Web e API health con HTTP 200,
readiness con PostgreSQL e Redis attivi e le quattro route tracking pubblicate e protette:
capability, anteprima, importazione e correzione manuale hanno restituito HTTP 401 senza
sessione, anziché 404 o 5xx. Il comando di avvio API esegue `alembic upgrade head` prima
di Uvicorn; il servizio Live attesta quindi l'applicazione della migrazione 0008. Il
collaudo non ha trasmesso credenziali e non ha modificato dati reali di Rionero.

Le prove automatiche coprono riconoscimento delle intestazioni, separazione
del campo combinato, precedenza dei valori separati, zeri iniziali, import multi-unità,
esito parziale, correzione manuale, persistenza dopo sync, isolamento fra scope,
autorizzazioni, audit, limiti di upload, parser CSV/XLS/XLSX e protezioni XML/ZIP. I file
principali sono `tests/test_order_tracking_api.py`,
`tests/test_order_tracking_migration.py`, `tests/test_orders_api.py` e
`apps/app-web/tests/orders-tracking.test.cjs`.

## Criteri verificati

I dieci criteri verificati del blocco sono:

| ID | Comportamento verificato nel SaaS |
| --- | --- |
| `LEGACY-UI-0254` | Lettura di export Kaufland CSV/XLSX/XLS nello scope selezionato, con anteprima ed errori espliciti |
| `LEGACY-UI-0255` | Proposta e correzione manuale della mappatura dei cinque campi |
| `LEGACY-UI-0256` | Import nell'archivio con conteggi separati di aggiornati, non abbinati e non validi |
| `LEGACY-UI-0257` | Scelta della specifica unità da completare con contesto ordine/prodotto |
| `LEGACY-UI-0258` | Corriere precompilato e modificabile senza cancellare un tracking omesso |
| `LEGACY-UI-0259` | Tracking precompilato, testuale e modificabile senza cancellare un corriere omesso |
| `LEGACY-UI-0260` | Salvataggio limitato all'unità autorizzata e risultato subito visibile |
| `LEGACY-TEST-0364` | Alias italiani e separazione esatta di `DPD | 08448875901263` |
| `LEGACY-TEST-0365` | Conservazione dei valori locali quando la sync API successiva restituisce vuoti |
| `LEGACY-TEST-0366` | Aggiornamento di tutte le unità abbinate al solo numero ordine |

Il blocco porta il ledger da 116 a 126 criteri verificati su 2.011 attivi:
`126 / 2.011 × 100 = 6,2655%`, arrotondato a **6,27%**.

## Limiti e pubblicazione

Questo blocco non completa invio tracking al marketplace, scadenziario, settlement,
ticket, listini, contabilità completa o connettori non verificati. Non viene dichiarato
alcun import reale dei file o degli ordini di Rionero: i test locali usano fixture e
dati sintetici.

**B20.3 è pubblicato nello staging.** Implementazione: `e36a646`; correzione finale del
risveglio API prima dell'accesso: `160fa00`. Web, API e worker sono stati verificati
`Live` sul commit `160fa001d120173d8200b43be72e4008139767f2`. La migrazione PostgreSQL
`20260908_0008`, readiness e protezione delle quattro route tracking sono operative.
L'hotfix auth aggiunge verifiche proprie ma non incrementa i criteri funzionali B20.3.
