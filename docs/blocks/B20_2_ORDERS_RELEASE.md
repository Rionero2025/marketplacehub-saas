# B20.2 / B21.2 — Filtri, selezione, totali e CSV degli ordini

Data: 7 settembre 2026. Perimetro: pannello Seller Enterprise, account Kaufland/Worten
già collegati. Streamlit originale in sola lettura; nessuna ripartizione degli utili.
Contratto sorgente: `B20_2_SELECTION_EXPORT_SOURCE_CONTRACT.md`.

## Funzioni trasferite

L'elenco ordini permette di scegliere un blocco operativo, conservarne le selezioni
tra le pagine, verificarne i totali economici ed esportarlo senza limitarsi alla pagina
visibile. Il marketplace dipende dall'account selezionato nel negozio attivo.

- Filtri multipli per stato, paese e valuta originale; vuoto esplicito significa nessuna
  riga. Corrieri vuoti significano tutti. Tracking e commissione hanno le scelte tutti,
  presente o mancante; una commissione zero è presente.
- Ricerca letterale per ordine, unità, SKU, EAN, nome, tracking e corriere; date secondo
  il giorno UTC e limiti inclusivi del venduto EUR. L'interfaccia inizializza i limiti
  dai dati disponibili secondo il comportamento originale.
- Selezione salvata per sessione, Seller, account, ambiente e firma del filtro. Una firma
  nuova parte con tutte le righe filtrate selezionate; tornando a una firma precedente
  si ritrova la sua scelta, privata degli ID che non appartengono più al blocco.
- Le pagine non cambiano la firma. Le singole checkbox conservano le scelte nelle altre
  pagine; seleziona/deseleziona tutti agisce sull'intero blocco filtrato.
- Riepilogo delle sole righe selezionate: numero di righe e ordini, quantità, venduto,
  commissioni, netto, costo, utile, percentuale sul costo, perdite e dati incompleti.
- Due CSV: righe selezionate nel filtro oppure intero blocco filtrato. UTF-8 con BOM,
  campi già disponibili nell'archivio, protezione delle celle testuali per i fogli di
  calcolo, importi negativi conservati come numeri. Con zero righe selezionate entrambi
  i download sono bloccati, come nella pagina originale.

I dati economici restano quelli del normalizzatore B20.1. Per ciascuna riga il totale
venduto/commissione/netto viene sommato soltanto quando tutti e tre sono noti; costo e
utile vengono sommati insieme soltanto quando entrambi sono noti. La percentuale totale
è `somma utile / somma costo × 100`, non la media delle percentuali delle righe.
Le cancellate Kaufland restano visibili con contributo economico nullo; `returned_paid`
Kaufland non viene reinterpretato come il rimborso Worten. Le regole contabili Worten
per annullamenti e rimborsi restano distinte e già definite nel contratto B20.1.

## Adattamento alla nuova architettura

La selezione viene salvata in SQL e al browser arrivano soltanto gli ID della pagina
corrente e i conteggi globali. Il backend verifica sessione, permesso `LOGISTICS`, negozio,
account, ambiente e firma anche per modifiche di selezione ed esportazioni. Il CSV è
generato a blocchi con ricontrollo delle autorizzazioni e senza payload remoti privati.

Streamlit applicava gli edit del suo `data_editor` tramite indici di riga. Il SaaS usa
UUID stabili e un comando specifico per la checkbox: l'ordinamento o il cambio pagina
non possono trasformare la scelta in una modifica di un altro ordine. Gli ID o gli edit
malformati, che l'helper originale ignorava, vengono **rifiutati dall'API con HTTP 422
senza alterare la selezione**. Un ID non autorizzato non può modificare altre selezioni.
Questa è un'equivalenza del comportamento operativo, non del protocollo Streamlit.

La migrazione `20260907_0007` aggiunge proiezioni ricercabili e tabelle delle selezioni,
conservando i record originali. Le proiezioni obsolete possono essere ricostruite dai
dati canonici anche se un worker della versione precedente termina durante il deploy.
Su PostgreSQL le operazioni di lettura/riepilogo e selezione usano una transazione con
snapshot coerente, blocco della selezione e retry dei conflitti di serializzazione.
Il collaudo locale SQLite non viene presentato come prova di concorrenza PostgreSQL.

## Verifiche eseguite

- **99 test frontend passati** e typecheck completato, inclusi ritorno di tutte le
  checkbox alla firma iniziale e inizializzazione dei limiti originali.
- QA indipendente dell'API tramite autenticazione, database SQL e worker reali;
  soltanto le API remote e la coda locale usano dati sintetici. Nessun server pubblico
  e nessuna richiesta reale al marketplace in queste prove.
- Dataset: 72 righe Kaufland e 6 Worten nel primo Seller; 3 righe Kaufland nel secondo.
  Paginazione 50+22, selezione 72→71→70 su due pagine, ripristino delle scelte per filtro,
  selezione/deselezione globale e isolamento fra sessioni e Seller.
- Filtri vuoti, commissione zero rispetto a commissione mancante, tracking presente e
  assente, EAN con zeri iniziali, ricerca letterale, estremi inclusivi e intervallo
  invertito rifiutato verificati dall'API.
- CSV di 70 righe selezionate e 72 filtrate: BOM, quoting, EAN preciso, testo che inizia
  con formula reso innocuo, numero negativo e assenza di dati privati verificati.
- Sync successiva: stessi 72 ID, variazione di 10 EUR nella riga aggiornata e rimozione
  di quella riga dalla selezione con range fisso 105 EUR, passata da 71 a 70 elementi.

La verifica API diretta senza limiti di importo permette di includere deliberatamente
anche le righe senza cambio EUR. Con tutti i dati selezionati prima del secondo sync:

| Account sintetico | Righe | Venduto EUR | Commissioni EUR | Netto EUR | Costo noto EUR | Utile noto EUR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Kaufland, primo Seller | 72 | 6.405,00 | 600,00 | 5.805,00 | 2.480,00 | 3.230,00 |
| Worten, primo Seller | 6 | 210,00 | 24,60 | 185,40 | 40,00 | 52,70 |
| Kaufland, secondo Seller | 3 | 210,00 | 20,00 | 190,00 | 80,00 | 110,00 |

Per Kaufland sono escluse dall'economia 9 cancellate; 3 righe hanno costo/utile
incompleti e una è in perdita. Per Worten 3 righe sono azzerate dalle regole di stato
e 2 hanno dati incompleti. I totali parziali restano espliciti. Le richieste e i worker
del collaudo SQLite `StaticPool` sono stati eseguiti in sequenza.

La verifica finale ha superato **300 test Python**, compresi i 16 test focalizzati sul
blocco, i **99 test frontend** disponibili al rilascio, typecheck e build di produzione.
Il collaudo browser ha verificato filtri, selezione tra pagine, riepilogo e download sul
pannello Seller. La suite frontend del repository è poi salita a **102/102** per la
correzione indipendente del cold start dell'accesso; questo aumento non aggiunge criteri
funzionali a B20.2.

## Criteri verificati

Si aggiungono soltanto i sei comportamenti originali di `tests/test_order_selection.py`:

| ID | Comportamento verificato nel SaaS |
| --- | --- |
| `LEGACY-TEST-0435` | Selezionare e deselezionare il blocco corrente senza cambiare altri filtri |
| `LEGACY-TEST-0436` | Le checkbox della pagina conservano le selezioni nelle altre pagine |
| `LEGACY-TEST-0437` | Un identificatore invalido non altera la selezione; il nuovo confine API risponde 422 |
| `LEGACY-TEST-0438` | La checkbox viene applicata all'ID stabile dell'ordine |
| `LEGACY-TEST-0439` | Più operazioni si accumulano e possono deselezionare |
| `LEGACY-TEST-0440` | Campi estranei o riferimenti malformati non producono edit di selezione; rifiuto API esplicito |

Prove riproducibili nel repository: `tests/test_order_selection.py`,
`tests/test_order_selection_migration.py`, `tests/test_orders_api.py`,
`apps/app-web/tests/orders-selection.test.cjs` e
`apps/app-web/tests/orders-actions-proxy.test.cjs`. Il collaudo indipendente con dataset
sintetico descritto sopra integra queste verifiche del comportamento.

Il ledger passa da **110 a 116 criteri verificati su 2.011 attivi: 5,77%**.
Baseline, requisiti originali e sei esclusioni D-013 restano invariati. Nessun criterio
generico di contabilità, tabella completa o Excel viene chiuso per la presenza del CSV.

## Limiti e pubblicazione

Non è dichiarata la parità con tutte le 36 colonne dell'export Ordini Streamlit:
scadenziario, disponibilità dei pagamenti, ritardi da ticket e relativi filtri non sono
ancora trasferiti. Restano pendenti listini, costi prioritari e fallback da listini
pubblicati, modifica/import tracking, altri connettori e contabilità completa. Il CSV
usa i campi attualmente implementati e non inventa le informazioni mancanti.

**B20.2 è pubblicato nello staging.** L'implementazione è nel commit `34a7932`; la
correzione finale della presentazione del pannello Ordini è nel commit `ccb19db`.
Web, API e worker sono stati verificati `Live` sul ramo finale, la migrazione
`20260907_0007` è stata applicata e readiness e protezione delle rotte sono risultate
operative. Il successivo commit auth `95fe6b5` non modifica le funzioni di B20.2.
Il collaudo usa dati sintetici: non attesta un'importazione reale di Rionero nello staging.
