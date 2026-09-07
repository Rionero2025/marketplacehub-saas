# B20.1 / B21.1 — Archivio ordini multicanale e navigazione Seller

Data: 7 settembre 2026. Perimetro: Seller Enterprise, senza limiti commerciali di pacchetto.
Il programma Streamlit originale resta in sola lettura. Le regole di riferimento e le
parti ancora mancanti sono documentate in `B20_1_ORDERS_SOURCE_CONTRACT.md`.

## Funzioni trasferite

La nuova macroarea **Ordini → Elenco ordini** usa l'account marketplace collegato al
negozio attivo. Sono disponibili Kaufland e Worten; il modulo non è fissato su Kaufland.
Il collegamento di un account resta un'operazione distinta dall'importazione degli ordini.

- Importazione in background con stato durevole, avanzamento, esito ed errori comprensibili.
- Limite operativo originale di 500, 1.000, 5.000 righe oppure tutte disponibili; non è
  un limite del pacchetto Enterprise.
- Kaufland: lettura esplicita degli otto stati, paginazione per stato, deduplicazione,
  ordinamento e limite globale, ambienti Live/Playground separati, dettagli degli spediti
  e recupero del tracking dal dettaglio ordine quando necessario.
- Worten: lettura generale degli ordini Mirakl OR11 per l'account selezionato, paginazione
  degli ordini e normalizzazione delle relative righe, con quantità maggiore di uno.
- Archivio persistente per organizzazione/Seller/account/ambiente/ordine/unità. Una nuova
  sincronizzazione aggiorna le righe già presenti senza duplicarle o svuotare lo storico.
- Nome prodotto, EAN, SKU composto, quantità, date, paese, stato, vendita, spedizione,
  commissione, netto da ricevere, costo acquisto, utile e percentuale sul costo.
- Ricerca letterale, filtri data/stato/paese, paginazione server e dettaglio della riga.
- Evidenza della fonte di costo, commissione e netto, importi originali e conversioni EUR,
  tracking/corriere e date effettive disponibili.

La permission `LOGISTICS` distingue consultazione e sincronizzazione. Il backend risolve
di nuovo scope e autorizzazioni durante il lavoro; una revoca o un account eliminato
interrompono il job. La coda contiene l'identificativo del job, non le credenziali. I
payload remoti rimangono privati; il browser riceve soltanto i campi previsti dal DTO.
I connettori effettuano letture: non spediscono, annullano o modificano ordini remoti.

## Economia e limiti dichiarati

Kaufland esprime gli importi in unità minime; Worten li esprime già nella valuta
principale. Il prezzo di riga Worten non viene moltiplicato due volte per la quantità.
I costi contenuti nello SKU sono EUR. Le regole di annullamento e rimborso restano quelle
descritte nel contratto originale, senza ripartizione degli utili tra gestore e partner.

Gli importi mancanti non diventano zero. In assenza di cambio BCE disponibile si conserva
la valuta originale, lasciando ignote le conversioni che non si possono calcolare. Non
è trasferito il vecchio bootstrap dei cambi fissi. Il costo da SKU è esplicito: il fallback
e la priorità dei listini pubblicati rimangono da completare. Una riga con dati mancanti
mostra un avviso, anche quando il download si conclude correttamente.

Il filtro data conserva la giornata UTC del codice Streamlit; le date visualizzate sono
in ora italiana e la pagina indica questa differenza. Le selezioni multiple stato/paese
del primo blocco non esauriscono tutte le regole di selezione della pagina originale.

Restano pendenti scadenziario e disponibilità dei pagamenti, ritardi da ticket, selezione
dei blocchi contabili, importazione/modifica manuale del tracking, CSV, filtri avanzati,
dati cliente operativi, stati dell'ordine fornitore e contabili, collegamenti ai listini
e altri connettori. La tabella disponibile non equivale alla parità completa della pagina
Ordini Streamlit. Nessuno di questi flussi è conteggiato come completato.

## Navigazione a due livelli — D-016

La barra scura contiene le macroaree **Panoramica, Ordini, Marketplace, Impostazioni**.
Il menu chiaro mostra soltanto le sottosezioni dell'area selezionata. Marketplace separa
Collega marketplace e Account collegati; Impostazioni separa Negozio, Organizzazioni e
Autorizzazioni. Le sottosezioni hanno percorsi autonomi e la navigazione evidenzia entrambi
i livelli. I vecchi collegamenti a sezioni del Seller vengono risolti nei nuovi percorsi.

La struttura segue la richiesta dell'utente e il riferimento pubblico
[accesso rapido di Base.com](https://base.com/en-EN/help/knowledgebase/quick-access/),
incluse le immagini pubblicate. Non è stato aperto un account privato Base.com: il riferimento
non prova l'aspetto completo della sua dashboard privata corrente. Il marchio e i dati
restano Marketplace Hub; il cambiamento del menu non aggiunge criteri operativi al ledger.

## Verifica eseguita

- Suite Python: **266 test passati**, di cui **85** relativi al nuovo blocco Ordini.
- Suite frontend: **80 test passati**.
- Build di produzione: **completata con esito positivo**.
- Collaudo browser desktop con autenticazione e repository SQL reali, coda locale che
  esegue il servizio worker reale e sole API marketplace simulate con dati sintetici.
- Kaufland: otto stati importati, nuova sincronizzazione ancora di otto righe con gli
  stessi ID; aggiornamento della vendita da 105 a 115, ricerca EAN di una sola riga e
  dettaglio con provenienza dei valori.
- Worten: quantità 2, venduto 105,00; commissione 12,30; netto 92,70; costo 40,00;
  utile 52,70. Nessuna doppia moltiplicazione del prezzo di riga.
- Isolamento del secondo Seller e controlli sulle autorizzazioni verificati nelle prove
  API; errori di download non eliminano l'archivio già salvato.
- Navigazione verificata in desktop e a 390/700 px: quattro macroaree, sottosezioni
  pertinenti, dati account, anagrafica e autorizzazioni in pagine separate. Sul mobile
  il cambio macroarea mantiene accessibili le sottosezioni; selezionarle chiude il menu.
  Verificato il ritorno alla larghezza desktop di 1.050 px.

La fixture locale contiene esclusivamente dati sintetici. Queste prove non dimostrano
un'importazione degli ordini reali di Rionero nello staging.

## Criteri verificati e percentuale

Sono aggiunti **23** criteri, conservativamente. Nessun requisito Master Spec generico
viene chiuso automaticamente perché esiste una nuova tabella o un DTO.

| ID | Evidenza del blocco |
| --- | --- |
| `LEGACY-TEST-0337` | Paginazione per stato e limite delle unità richieste |
| `LEGACY-TEST-0338` | Enum Kaufland degli otto stati e dettagli degli stati spediti |
| `LEGACY-TEST-0339` | Importi Kaufland, vendita, commissione e netto |
| `LEGACY-TEST-0340`–`0347` | Esempi originali SKU, parser da destra, costo/utile, codice opaco, EAN diverso e costo ignoto |
| `LEGACY-TEST-0360` | Priorità della commissione API esplicita |
| `LEGACY-TEST-0362`, `0363` | Tracking in lista e spedizioni annidate |
| `LEGACY-TEST-0367` | Ricerca dell'unità corretta nel dettaglio ordine |
| `LEGACY-TEST-0368`, `0369` | Conversione degli importi al tasso EUR e costo SKU già in EUR |
| `LEGACY-TEST-0376`, `0377` | Sincronizzazione, persistenza, dettaglio spedizione e fallback ordine |
| `LEGACY-UI-0252`, `0253` | Scelta account e ambiente Playground |
| `LEGACY-UI-0261`, `0264` | Ricerca e scelta del marketplace tramite l'account integrato |

Evidenza automatizzata: `tests/test_order_normalization.py`, `tests/test_order_connectors.py`,
`tests/test_orders_api.py`, `tests/test_orders_migration.py`, `apps/app-web/tests/orders.test.cjs`.
Le interazioni UI sono state controllate anche nel browser come descritto sopra.

Il ledger passa da **87 a 110 criteri verificati**, su **2.011** attivi:
`110 / 2.011 × 100 = 5,4699%`, arrotondato a **5,47%**. La baseline di 2.017 criteri e le
sei esclusioni esplicite D-013 restano invariate. Il numero dei test verdi non è la
percentuale di completamento del prodotto.

## Pubblicazione

Stato: **da verificare**. Prima di dichiarare online il blocco occorre verificare il
commit pubblicato e il rilascio di web, API e worker su Render, la migrazione Ordini e
la readiness dei servizi. Commit e identificativi di deploy verranno aggiunti dopo
il controllo. L'importazione con credenziali reali nello staging non è ancora attestata.
