# B20.4 — Scadenziario pagamenti e ritardi ticket degli ordini

Data: 10 settembre 2026. Perimetro: pannello Seller Enterprise, senza limiti
commerciali di pacchetto. Il programma Streamlit originale resta in sola lettura.
Contratto sorgente: `B20_4_PAYMENTS_SOURCE_CONTRACT.md`.

Stato del documento: **PUBBLICATO E COLLAUDATO NELLO STAGING**.

Questo documento attesta il rilascio del blocco, le verifiche locali, il collaudo su
Render e l'aggiornamento del ledger.

## Funzioni trasferite

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

Per gli altri marketplace il blocco non applica le scadenze Kaufland: espone che il
programma pagamenti non è disponibile finché non viene definito il relativo adattatore.

## Persistenza e compatibilità

La migrazione `20260910_0009_order_payments.py` aggiunge alle righe ordine le
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

## Interfaccia Seller pubblicata

Stato frontend: **CONGELATO, PUBBLICATO E VERIFICATO**.

Il rilascio mostra nell'archivio Ordini:

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

Conteggio test frontend: **136/136 superati**. Il controllo TypeScript è incluso nella
suite ed è superato. Build Next.js produzione: **superata, 17/17 pagine generate**.

## Verifiche locali acquisite

Risultati acquisiti sul backend pubblicato:

- suite Python completa: **401/401 test superati**;
- suite pertinente a pagamenti, connettori, Ordini, selezioni, tracking e migrazione:
  **215/215**, incluse **14/14** prove della migrazione;
- Ruff: **superato**.

Altri gate locali acquisiti:

- suite frontend completa: **136/136 test superati**;
- controllo TypeScript e build Next.js: **superati; 17/17 pagine generate**;
- `pip check` e `git diff --check`: **superati**; `pip-audit` sul lock e `pnpm audit`:
  **nessuna vulnerabilità nota**;
- unica head Alembic **`20260910_0009`**; upgrade, ripresa, downgrade e roundtrip
  0008→0009→0008 coperti nelle **14/14** prove della migrazione;
- sorgenti Streamlit del blocco verificate in sola lettura contro il manifest v271:
  **3/3 hash SHA-256 coincidenti** (`kaufland_orders.py`, pagina Ordini e relativi test).
  Il resto dell'albero Streamlit presenta modifiche esterne al perimetro B20.4 e non viene
  riscritto né dichiarato invariato da questo rilascio.

Commit implementazione congelato: **`e83bc3209c8ae5bab0b58fb9edf8c777f0df08f1`**.
Correzione finale della leggibilità economica: **`ca7eb3651f19118ae43eda21b66032bbd3dd8ec4`**.

## Collaudo staging completato

Pubblicazione Render: **COMPLETATA E VERIFICATA**.

Il collaudo del 10 settembre 2026 ha verificato sul commit finale:

1. Web, API e worker nello stato `Live` sul medesimo hash completo;
2. comando `alembic upgrade head` concluso prima dell'avvio API e head
   `20260910_0009` applicata;
3. health Web HTTP 200 e readiness API HTTP 200 con PostgreSQL e Redis `up`;
4. archivio Ordini, filtro pagamento e nuova lettura dei totali economici raggiungibili
   nello scope Seller; il controllo visivo è stato confermato dal Seller;
5. dati pagamento/ticket verificati nel DTO, nel dettaglio, nelle due selezioni e nel
   CSV dalle prove di accettazione pubblicate sullo stesso commit;
6. lista, dettaglio, sincronizzazione, selezione, export e capability tracking protetti:
   le prove anonime live restituiscono HTTP 401, mai 404 o 5xx inattesi;
7. ricalcolo temporale senza nuova sincronizzazione e isolamento fra tenant, account e
   ambiente coperti dalle prove backend complete;
8. nessuna credenziale reale trasmessa e nessuna scrittura sui dati di Rionero durante
   il collaudo automatico.

Commit pubblicato e verificato da Web, API e worker:
**`ca7eb3651f19118ae43eda21b66032bbd3dd8ec4`**. Collaudo concluso il
**10 settembre 2026 alle 18:18 CEST**.

## Criteri verificati

I 21 criteri sono verificati individualmente e registrati nel ledger con questo
documento come evidenza.

| ID | Evidenza acquisita | Stato rilascio |
| --- | --- | --- |
| `LEGACY-TEST-0348` | Consegna +14 con tracking e importi invariati | `verified` |
| `LEGACY-TEST-0349` | Ricezione esplicita distinta dall'aggiornamento/rilascio | `verified` |
| `LEGACY-TEST-0350` | Autopaid con timestamp effettivo, fonte e disponibilità | `verified` |
| `LEGACY-TEST-0351` | Nessun uso del timestamp generico come ricezione | `verified` |
| `LEGACY-TEST-0352` | Riparazione completa del vecchio fallback dal payload archiviato | `verified` |
| `LEGACY-TEST-0353` | Countdown futuro/oggi/disponibile su calendario UTC | `verified` |
| `LEGACY-TEST-0354` | Priorità della data effettiva sulla stima | `verified` |
| `LEGACY-TEST-0355` | Spedizione +21 senza tracking | `verified` |
| `LEGACY-TEST-0356` | Attesa della consegna quando il tracking è presente | `verified` |
| `LEGACY-TEST-0357` | Rinvio per durata del ticket chiuso | `verified` |
| `LEGACY-TEST-0358` | Ticket aperto, data provvisoria e netto non disponibile | `verified` |
| `LEGACY-TEST-0359` | Durata/conteggio di ticket aperti e chiusi, intervalli uniti | `verified` |
| `LEGACY-TEST-0361` | Arricchimento completo della riga legacy, inclusa commissione, senza dipendere dalle nuove colonne | `verified` |
| `LEGACY-TEST-0370` | Ultima data del blocco e cancellazioni escluse | `verified` |
| `LEGACY-TEST-0371` | Segnalazione delle righe prive di data | `verified` |
| `LEGACY-TEST-0372` | Blocco autopaid interamente disponibile | `verified` |
| `LEGACY-TEST-0373` | Totali delle sole unità scelte, disponibile/attesa separati | `verified` |
| `LEGACY-TEST-0374` | Cancellazioni escluse e costo sconosciuto esplicito | `verified` |
| `LEGACY-UI-0263` | Tabella selezionabile con campi pagamento/ticket e riepilogo coerente | `verified` |
| `MASTER-0366` | Date previste di pagamento operative e comprensibili | `verified` |
| `MASTER-0821` | Regola di previsione vincolata al marketplace verificato | `verified` |

Il blocco porta il ledger da 126 a 147 criteri verificati su 2.011 attivi:
`147 / 2.011 × 100 = 7,3098%`, arrotondato a **7,31%**.

## Limiti del rilascio

Il blocco non riconcilia il booking report, non conferma l'accredito bancario e non
gestisce regole di pagamento di marketplace diversi da Kaufland. Le date stimate
dipendono dalla qualità degli eventi e dei ticket ricevuti dall'API. Uno snapshot ticket
non aggiornato viene dichiarato nel job e può rendere temporaneamente superata la stima
finché la sincronizzazione successiva non riesce.

**B20.4 è pubblicato nello staging.** Implementazione: `e83bc32`; correzione finale della
leggibilità economica: `ca7eb36`. Web, API e worker sono stati verificati `Live` sul commit
`ca7eb3651f19118ae43eda21b66032bbd3dd8ec4`; migrazione `20260910_0009`, readiness e
protezioni delle route Ordini sono operative.
