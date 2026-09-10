# B20.4 — Scadenziario pagamenti e ritardi ticket degli ordini

Data: 10 settembre 2026. Perimetro: pannello Seller Enterprise, senza limiti
commerciali di pacchetto. Il programma Streamlit originale resta in sola lettura.
Contratto sorgente: `B20_4_PAYMENTS_SOURCE_CONTRACT.md`.

Stato del documento: **BOZZA — NON PUBBLICATO E NON COLLAUDATO NELLO STAGING**.

Questo documento descrive il candidato al rilascio locale. Non autorizza ancora
l'aggiornamento del ledger e non dichiara operative le funzioni su Render.

## Funzioni trasferite nel candidato locale

La macroarea **Ordini** estende l'archivio server-side con lo scadenziario Kaufland
originale:

- separazione esatta fra evento di consegna, evento di spedizione e rilascio effettivo
  del netto; un aggiornamento generico non diventa una falsa consegna;
- priorità della data effettiva comunicata da Kaufland;
- stima consegna + 14 giorni quando è presente il tracking;
- stima spedizione + 21 giorni quando il tracking è assente;
- disponibilità confermata anche senza data per lo stato `sent_and_autopaid`;
- countdown per data di calendario UTC, ricalcolato quando l'archivio viene letto;
- ritardo derivato dai ticket chiusi e data provvisoria mentre almeno un ticket resta
  aperto, con unione degli intervalli sovrapposti;
- riparazione delle vecchie righe dal payload API archiviato;
- filtri server-side per disponibile, in attesa, data non determinabile e ticket aperto;
- proiezioni nel dettaglio e nell'export CSV;
- data più tarda, completezza delle date, importi disponibili/in attesa e costi ignoti
  nel riepilogo server-side della selezione persistente.

Il connettore Kaufland tenta di acquisire, insieme agli ordini, uno snapshot paginato
dei ticket nei cinque stati previsti. L'errore dei ticket non annulla la sincronizzazione
ordini e conserva lo snapshot precedente. La sostituzione di uno snapshot completo è
atomica e limitata a organizzazione, Seller, account e ambiente.

Per gli altri marketplace il candidato non applica le scadenze Kaufland: espone che il
programma pagamenti non è disponibile finché non viene definito il relativo adattatore.

## Persistenza e compatibilità

La migrazione candidata `20260910_0009_order_payments.py` aggiunge alle righe ordine le
proiezioni per data, disponibilità, definitività, ticket aperto e durata del ritardo.
Crea inoltre `seller_order_payment_tickets`, con chiave univoca nello scope
Seller/account/ambiente e indice comprensivo dell'organizzazione.

L'upgrade ripara in batch le righe pregresse dal dato canonico e dal payload provider
archiviato e conserva un backup puntuale degli eventi modificati. Elenco, dettaglio ed
export ricalcolano in memoria soltanto le righe richieste: una GET non esegue scritture
né scansioni dell'intero archivio. Le proiezioni vengono materializzate durante sync,
aggiornamento dello snapshot ticket e migrazione. Il vero upsert provider annulla un
eventuale backup ormai superato. Il downgrade ripristina la presenza e il valore degli
eventi precedenti, rimuove i campi pagamento dal JSON canonico e le sole strutture del
blocco.

Per impedire divergenze durante un deploy progressivo, ogni scrittura Kaufland deve
aggiornare insieme `updated_at` e `payment_projection_updated_at`. PostgreSQL installa il
vincolo inizialmente `NOT VALID`, prima del backfill, e lo valida a riparazione conclusa;
SQLite usa trigger equivalenti installati prima del backfill, senza ricreare la tabella e
senza perdere righe figlie. Un vecchio worker successivo alla migrazione viene quindi
rifiutato atomicamente, mentre il nuovo worker continua a scrivere sullo schema 0008.

## Interfaccia Seller candidata

Stato frontend locale: **CONGELATO E VERIFICATO; COLLAUDO LIVE ANCORA DA ESEGUIRE**.

Il rilascio dovrà mostrare nell'archivio Ordini:

- il filtro «Stato pagamento»;
- la sezione «Date previste di pagamento» con le colonne di evento, stima, importi e
  ticket definite nel contratto sorgente;
- seconda selezione per riga, indipendente e inizialmente vuota, limitata alle righe
  della selezione Ordini, con comandi seleziona/deseleziona tutto nello scope filtrato;
- riepilogo delle sole righe selezionate, con data finale, netto disponibile, netto in
  attesa, righe cancellate e costi non calcolabili;
- messaggi distinti per data completa, data parziale, nessuna data e blocco interamente
  disponibile;
- comportamento responsive e accessibile verificato prima della pubblicazione.

Conteggio test frontend: **135/135 superati**. Il controllo TypeScript è incluso nella
suite ed è superato. Build Next.js produzione: **superata, 17/17 pagine generate**.

## Verifiche locali acquisite

Risultati acquisiti sul candidato backend corrente:

- suite Python completa: **401/401 test superati**;
- suite pertinente a pagamenti, connettori, Ordini, selezioni, tracking e migrazione:
  **215/215**, incluse **14/14** prove della migrazione;
- Ruff: **superato**.

Altri gate locali acquisiti:

- suite frontend completa: **135/135 test superati**;
- controllo TypeScript e build Next.js: **superati; 17/17 pagine generate**;
- `pip check` e `git diff --check`: **superati**; `pip-audit` sul lock e `pnpm audit`:
  **nessuna vulnerabilità nota**;
- unica head Alembic **`20260910_0009`**; upgrade, ripresa, downgrade e roundtrip
  0008→0009→0008 coperti nelle **14/14** prove della migrazione;
- sorgenti Streamlit del blocco verificate in sola lettura contro il manifest v271:
  **3/3 hash SHA-256 coincidenti** (`kaufland_orders.py`, pagina Ordini e relativi test).
  Il resto dell'albero Streamlit presenta modifiche esterne al perimetro B20.4 e non viene
  riscritto né dichiarato invariato da questo rilascio.

Commit implementazione congelato: **[DA COMPILARE]**.

## Collaudo staging ancora necessario

Pubblicazione Render: **NON ESEGUITA / NON CONFERMATA IN QUESTA BOZZA**.

Prima di pubblicare il documento occorre verificare sullo stesso commit:

1. Web, API e worker nello stato `Live`;
2. migrazione Alembic `20260910_0009` applicata;
3. health Web/API HTTP 200 e readiness con PostgreSQL e Redis attivi;
4. archivio Ordini e filtro pagamento raggiungibili nello scope Seller;
5. dati pagamento/ticket presenti nel DTO, nel dettaglio, nella selezione e nel CSV;
6. route protette senza sessione con 401/403, mai 404 o 5xx inattesi;
7. ricalcolo temporale senza nuova sincronizzazione e isolamento fra tenant/account/
   ambiente;
8. nessun uso di credenziali reali nel collaudo automatico.

Commit pubblicato: **[DA COMPILARE]**. Hash completo verificato da Web/API/worker:
**[DA COMPILARE]**. Data e ora del collaudo: **[DA COMPILARE]**.

## Criteri candidati, non ancora chiusi

I 21 criteri candidati restano `pending`; non sono stati scritti nel ledger da questa
bozza.

| ID | Evidenza richiesta prima della chiusura | Stato bozza |
| --- | --- | --- |
| `LEGACY-TEST-0348` | Consegna +14 con tracking e importi invariati | `pending` |
| `LEGACY-TEST-0349` | Ricezione esplicita distinta dall'aggiornamento/rilascio | `pending` |
| `LEGACY-TEST-0350` | Autopaid con timestamp effettivo, fonte e disponibilità | `pending` |
| `LEGACY-TEST-0351` | Nessun uso del timestamp generico come ricezione | `pending` |
| `LEGACY-TEST-0352` | Riparazione completa del vecchio fallback dal payload archiviato | `pending` |
| `LEGACY-TEST-0353` | Countdown futuro/oggi/disponibile su calendario UTC | `pending` |
| `LEGACY-TEST-0354` | Priorità della data effettiva sulla stima | `pending` |
| `LEGACY-TEST-0355` | Spedizione +21 senza tracking | `pending` |
| `LEGACY-TEST-0356` | Attesa della consegna quando il tracking è presente | `pending` |
| `LEGACY-TEST-0357` | Rinvio per durata del ticket chiuso | `pending` |
| `LEGACY-TEST-0358` | Ticket aperto, data provvisoria e netto non disponibile | `pending` |
| `LEGACY-TEST-0359` | Durata/conteggio di ticket aperti e chiusi, intervalli uniti | `pending` |
| `LEGACY-TEST-0361` | Arricchimento completo della riga legacy, inclusa commissione, senza dipendere dalle nuove colonne | `pending` |
| `LEGACY-TEST-0370` | Ultima data del blocco e cancellazioni escluse | `pending` |
| `LEGACY-TEST-0371` | Segnalazione delle righe prive di data | `pending` |
| `LEGACY-TEST-0372` | Blocco autopaid interamente disponibile | `pending` |
| `LEGACY-TEST-0373` | Totali delle sole unità scelte, disponibile/attesa separati | `pending` |
| `LEGACY-TEST-0374` | Cancellazioni escluse e costo sconosciuto esplicito | `pending` |
| `LEGACY-UI-0263` | Tabella selezionabile con campi pagamento/ticket e riepilogo coerente | `pending` |
| `MASTER-0366` | Date previste di pagamento operative e comprensibili | `pending` |
| `MASTER-0821` | Regola di previsione vincolata al marketplace verificato | `pending` |

Il ledger ufficiale resta quindi a **126/2.011 = 6,27%**. La percentuale potrà cambiare
soltanto dopo congelamento, test completi, pubblicazione, collaudo e valutazione
individuale delle 21 evidenze.

## Limiti della bozza

Il candidato non riconcilia il booking report, non conferma l'accredito bancario e non
gestisce regole di pagamento di marketplace diversi da Kaufland. Le date stimate
dipendono dalla qualità degli eventi e dei ticket ricevuti dall'API. Uno snapshot ticket
non aggiornato viene dichiarato nel job e può rendere temporaneamente superata la stima
finché la sincronizzazione successiva non riesce.

**B20.4 non è ancora pubblicato.** Questa frase deve essere sostituita soltanto dopo il
collaudo staging e l'aggiornamento contestuale del ledger e dei documenti di avanzamento.
