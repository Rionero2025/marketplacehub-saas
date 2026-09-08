# B20.3 — Importazione e correzione tracking nell'archivio ordini

Fonti originali lette in sola lettura:

- `C:/Users/Giorgio/Documents/marketplace_hub/pages/3_Ordini_Kaufland.py`, righe 390–595;
- `C:/Users/Giorgio/Documents/marketplace_hub/services/kaufland_orders.py`, funzioni
  `detect_tracking_columns`, `split_shipment_text`, `save_order_tracking`,
  `import_order_tracking` e `upsert_order_unit`;
- `C:/Users/Giorgio/Documents/marketplace_hub/tests/test_kaufland_orders.py`, righe
  636–710 (`LEGACY-TEST-0364`–`0366`).

Il contratto trasferisce il comportamento operativo della pagina Streamlit nel pannello
Seller Enterprise. La prima parità verificabile riguarda Kaufland perché queste sono le
fonti originali disponibili. L'interfaccia SaaS deve legare l'azione al marketplace e
all'account selezionati a monte; un connettore diverso potrà esporre la stessa funzione
solo dopo avere definito e verificato il proprio formato. Questo blocco aggiorna
l'archivio interno e **non invia tracking al marketplace**.

## Matrice del comportamento

| Input | Trasformazione | Persistenza | Output osservabile |
| --- | --- | --- | --- |
| File `.csv` | Tutte le celle sono lette come testo; separatore rilevato come nell'originale; valori vuoti non diventano `NaN` | Nessuna scrittura durante la sola lettura | Numero righe e anteprima delle prime 10 righe |
| File `.xlsx` o `.xls` | Tutte le celle sono lette come testo e le intestazioni sono ripulite dagli spazi esterni | Nessuna scrittura durante la sola lettura | Stessa anteprima e stessa fase di mappatura del CSV |
| Intestazioni del file | Unicode NFKD, rimozione degli accenti, minuscolo e rimozione di tutto ciò che non è alfanumerico; per ogni campo vale la prima intestazione riconosciuta | La mappatura proposta non modifica gli ordini | Proposta modificabile per ID unità, numero ordine, corriere, tracking e campo combinato |
| Mappatura manuale | L'utente può scegliere qualunque colonna o `—`; il riconoscimento automatico imposta soltanto il valore iniziale | La mappatura vale per l'import corrente e per il contesto account/ambiente dell'interfaccia | I cinque controlli mostrano l'associazione effettiva prima dell'import |
| Campo combinato, per esempio `DPD | 08448875901263` | Separazione sul primo delimitatore disponibile, nell'ordine `|`, ` - `, `;`; senza delimitatore l'intero valore è tracking e il corriere resta vuoto | Corriere/tracking estratti alimentano la stessa scrittura dei campi separati | L'anteprima dell'esito conta le unità aggiornate |
| Campi separati insieme al campo combinato | Un corriere o tracking separato e non vuoto ha priorità; il campo combinato completa soltanto ciò che manca | I valori finali non vuoti sostituiscono i rispettivi campi archiviati | Nessuna duplicazione tra valore separato e combinato |
| Più tracking separati da virgola | Suddivisione, trim, eliminazione dei vuoti e deduplicazione conservando l'ordine; serializzazione con `, ` | Un valore tracking non vuoto sostituisce quello archiviato | Tracking normalizzato e ricercabile nell'archivio |
| Riga con ID unità | L'ID unità ha priorità anche quando la riga contiene anche il numero ordine | Aggiornamento della sola unità nello scope Seller/account/ambiente | `updated` cresce di una unità se il target esiste |
| Riga senza ID unità ma con numero ordine | Matching per numero ordine | Aggiornamento di **tutte** le unità di quell'ordine nello stesso scope | `updated` cresce del numero effettivo di unità aggiornate |
| Riga senza identificativo oppure senza corriere/tracking | Classificazione come non valida; nessun tentativo di matching | Nessuna modifica per quella riga | Elemento in `invalid` con numero riga e motivo |
| Identificativo valido nel formato ma assente dall'archivio | Matching senza risultati | Nessuna modifica | Elemento in `unmatched` con numero riga e identificativi ricevuti |
| Correzione manuale | Scelta di una unità dall'archivio; campi precompilati dai valori correnti; trim e normalizzazione tracking | Aggiornamento per ID unità, sempre limitato a Seller/account/ambiente | Successo se la riga esiste; errore esplicito se non esiste |
| Scrittura con un solo campo non vuoto | Il campo vuoto non cancella il valore già archiviato | Aggiornamento indipendente di corriere e tracking più timestamp di modifica | Il valore non modificato resta visibile |
| Sincronizzazione API successiva con corriere/tracking vuoti | L'upsert considera vuoti i nuovi dati di spedizione | Conservazione dei valori importati o manuali già presenti | Corriere e tracking restano disponibili dopo il sync |
| Sincronizzazione API successiva con valori non vuoti | I valori reali restituiti dall'API sono accettati dall'upsert | Sostituzione dei rispettivi valori archiviati | L'archivio mostra i dati più recenti ricevuti dall'API |

La frase originale «i valori restano salvati anche dopo le sincronizzazioni successive»
va interpretata con precisione: i valori locali sopravvivono quando il sync successivo
non fornisce quel campo. L'upsert originale sostituisce invece corriere o tracking se
l'API restituisce un nuovo valore non vuoto. La parità SaaS deve conservare questa regola.

## Alias riconosciuti

Il confronto avviene dopo la normalizzazione dell'intestazione descritta sopra.

| Campo canonico | Alias originali |
| --- | --- |
| `id_order_unit` | `idorderunit`, `orderunitid`, `orderunit`, `unitaordine`, `unitaordineid`, `idunitaordine`, `bestellpositionid`, `orderitemid` |
| `id_order` | `idorder`, `orderid`, `ordernumber`, `ordine`, `numeroordine`, `bestellnummer`, `bestellung`, `kauflandorderid` |
| `carrier_code` | `carriercode`, `carrier`, `carriername`, `corriere`, `spedito con`, `speditocon`, `versanddienstleister`, `shippingprovider`, `shippingcarrier` |
| `tracking_numbers` | `trackingnumbers`, `trackingnumber`, `tracking`, `trackingcode`, `numerotracking`, `numerospedizione`, `tracciabilita`, `sendungsnummer`, `paketnummer`, `parcelnumber` |
| `combined_shipment` | `shipment`, `shipmentinformation`, `shippinginformation`, `informazionispedizione`, `spedizione`, `versandinformation` |

Gli ID e i tracking restano stringhe: zeri iniziali e codici alfanumerici non devono
essere convertiti in numeri. Il parser non valuta formule contenute nel file.

## Limiti tecnici pubblicati

I limiti sono applicati allo stesso modo in anteprima e durante l'importazione. Il
server espone al frontend, tramite le capability del contesto Seller/account, formato,
dimensione, righe, colonne, celle, lunghezza delle celle e ampiezza dell'anteprima; il
browser mostra i limiti principali prima della scelta del file. Le altre soglie della
tabella restano parte del contratto API/core. Il file viene elaborato in memoria e non
viene archiviato.

| Risorsa | Limite |
| --- | ---: |
| Formati accettati | CSV, XLSX e XLS |
| Dimensione del file | 5 MiB (`5 × 1.024 × 1.024` byte) |
| Righe dati | 10.000, oltre alla riga di intestazione |
| Colonne | 100 |
| Celle logiche | 200.000, intestazioni incluse |
| Lunghezza di una cella | 2.000 caratteri |
| Lunghezza di una intestazione | 200 caratteri |
| Lunghezza di un identificativo ordine/unità | 200 caratteri |
| Unità ordine aggiornabili da un singolo import | 10.000 |
| Righe mostrate nell'anteprima | 10 |

Il conteggio delle 200.000 celle considera il rettangolo logico importato: ogni riga
vale per il numero di colonne dell'intestazione anche quando contiene celle finali
vuote. Nel formato XLS/XLSX la riga di intestazione concorre al totale; nel CSV il
contatore parte dalle intestazioni e aggiunge tutte le righe dati materializzate. Una
riga fisicamente vuota del CSV viene ignorata, mentre una riga delimitata ma con valori
vuoti resta una riga dati e viene poi classificata secondo le regole dell'import.

## Protezioni interne del parser XLSX

Prima di affidare il file a `openpyxl`, il backend controlla l'archivio ZIP e analizza
in streaming le parti XML con `defusedxml`. DTD, entità ed entità esterne sono vietati;
sono accettate soltanto entry non cifrate con compressione ZIP `stored` o `deflated`.
Le soglie interne sono:

| Parte o struttura XLSX | Protezione applicata |
| --- | --- |
| Archivio ZIP | Massimo 1.000 entry e 50 MiB complessivi non compressi; anche la lettura effettiva cumulativa delle parti XML/RELS è fermata a 50 MiB |
| Token XML | Massimo 64 KiB per tag/dichiarazione/commento/CDATA/processing instruction e 8 KiB per singolo valore di attributo |
| Budget XML condiviso | Massimo 1.000.000 di elementi, cumulativi tra tutte le parti XML effettivamente attraversate dai guard |
| `sharedStrings.xml` | Massimo 10 MiB, 100.000 stringhe, 2.000 caratteri e 256 elementi XML per stringa, 5 MiB di testo e 500.000 elementi complessivi |
| `styles.xml` | Massimo 2 MiB e 10.000 elementi |
| Parti globali, `workbook.xml` e relazioni | Massimo 2 MiB, 10.000 elementi e 2 MiB di testo per parte |
| Nomi definiti del workbook | Massimo 2.000 nomi e 2.000 caratteri per nome |
| Primo foglio | Massimo 4.100.000 elementi locali, comunque subordinati al budget XML condiviso di 1.000.000; 5 MiB di testo, 256 elementi per cella e gli stessi limiti pubblici di righe, colonne, celle e caratteri |
| Altri fogli | Il solo preambolo letto da `openpyxl` è controllato fino a `dimension` o `sheetData`, con massimo 10.000 elementi e 256 KiB di testo |

Coordinate mancanti vengono ricostruite in sequenza; coordinate duplicate, retrograde,
incoerenti con la riga o dimensioni non valide fanno rifiutare il file. I limiti di
righe, colonne e celle si applicano alla dimensione dichiarata e a quella realmente
osservata, così un foglio apparentemente sparso non può espandere una griglia enorme.

La semantica funzionale resta quella del programma originale: viene importato
**soltanto il primo foglio nell'ordine del workbook**, anche quando Excel ne ha salvato
un altro come attivo. I fogli successivi non sono sorgenti di tracking e quindi non
ricevono i limiti semantici di righe/colonne/celle del primo foglio; il loro preambolo,
l'archivio complessivo e le dichiarazioni XML restano comunque protetti. Per XLS viene
letto soltanto il primo foglio e valgono i limiti pubblicati; per CSV viene effettuato
anche un controllo della forma prima che il reader allochi le celle.

## Adattamento alla nuova architettura SaaS

Ogni operazione richiede una sessione Seller valida, permesso `LOGISTICS`, negozio
attivo, account collegato e ambiente autorizzato. Il backend ricava Seller e permessi
dalla sessione; non accetta dal browser uno scope arbitrario. Il matching usa
l'identità stabile della riga e deve essere vincolato contemporaneamente a Seller,
account marketplace e ambiente. Un ordine omonimo di un altro Seller, account o
ambiente non può essere letto o aggiornato.

Il file viene validato e trasformato sul server. Il browser riceve anteprima, mappatura
proposta e riepilogo, senza payload remoto privato o credenziali. L'applicazione aggiorna
insieme dato canonico e proiezioni ricercabili usate da tabella, filtri, dettaglio e CSV;
la selezione B20.2 resta associata agli stessi UUID e non viene ricreata. Le scritture
producono un evento di audit con attore, scope, origine `portal_import` o `manual` e
timestamp. Questa provenienza aggiunge tracciabilità al SaaS senza cambiare il risultato
dell'algoritmo originale.

L'import è a esito parziale per riga: le righe valide e abbinate vengono applicate;
`invalid` e `unmatched` non mascherano gli aggiornamenti riusciti. Il riepilogo distingue
righe del file da unità aggiornate, perché una riga identificata dal numero ordine può
aggiornare più unità.

## Stati ed errori

| Condizione | Stato/risposta richiesta | Effetto sui dati |
| --- | --- | --- |
| File valido, solo anteprima | `ready_for_mapping` con righe lette, intestazioni, prime 10 righe e proposta | Nessuno |
| Estensione non supportata, file illeggibile o struttura priva di colonne | Errore di lettura specifico; nel confine API errore 422 | Nessuno |
| File oltre i limiti tecnici pubblicati | Errore 413, senza tentare un'elaborazione parziale | Nessuno |
| Manca sia ID unità sia numero ordine nella mappatura | «Associa almeno la colonna Numero ordine oppure ID unità ordine» | Nessuno |
| Mancano tracking, corriere e campo combinato nella mappatura | «Associa almeno Tracking, Corriere oppure il campo combinato» | Nessuno |
| Riga priva di identificativo | `invalid`: «Identificativo ordine assente» | Nessuno per la riga |
| Riga priva di entrambi i dati di spedizione | `invalid`: «Tracking/corriere assente» | Nessuno per la riga |
| Target non trovato nello scope | `unmatched` con numero riga, unità e ordine | Nessuno per la riga |
| Import con almeno una unità aggiornata | `completed` o `completed_with_warnings`; conteggi `updated`, `unmatched`, `invalid` | Solo target validi e autorizzati |
| Import senza aggiornamenti | Esito esplicito con `updated = 0`; mai un falso messaggio di successo | Nessuno |
| Salvataggio manuale senza identificativo | Errore 422 equivalente a «Indica l'ID unità ordine oppure il numero ordine» | Nessuno |
| Salvataggio manuale senza corriere e tracking | Errore 422 equivalente a «Indica almeno il corriere oppure il tracking» | Nessuno |
| Unità manuale non presente | 404 nel nuovo confine API; interfaccia «L'unità ordine non è presente nell'archivio» | Nessuno |
| Sessione assente/scaduta o scope non autorizzato | 401/403 senza rivelare se un ID esiste in un altro scope | Nessuno |
| Conflitto o errore interno | Errore recuperabile con request ID; nessun conteggio confermato per scritture non concluse | Nessuna scrittura non confermata |

## Dieci criteri candidati, ancora da verificare

Il solo contratto non cambia il ledger. I dieci criteri restano `pending` fino a quando
implementazione, test API/core, test frontend e collaudo del flusso reale ne dimostrano
il comportamento nel SaaS.

| ID | Prova necessaria per la chiusura |
| --- | --- |
| `LEGACY-UI-0254` | Il pannello Seller accetta un export Kaufland CSV/XLSX nello scope dell'account selezionato e mostra anteprima/messaggi di lettura |
| `LEGACY-UI-0255` | La mappatura propone le colonne riconosciute e permette di correggere ID unità, ordine, corriere, tracking e campo combinato prima dell'import |
| `LEGACY-UI-0256` | «Importa tracking nell'archivio» applica il file e mostra separatamente unità aggiornate, righe non abbinate e righe non valide |
| `LEGACY-UI-0257` | «Ordine da completare» permette di scegliere una specifica unità dell'archivio corrente con contesto ordine/prodotto |
| `LEGACY-UI-0258` | Il campo manuale «Corriere» è precompilato e consente una correzione persistente senza cancellare un tracking lasciato vuoto |
| `LEGACY-UI-0259` | Il campo manuale «Tracking» è precompilato, conserva testo/zeri iniziali e consente una correzione persistente senza cancellare il corriere lasciato vuoto |
| `LEGACY-UI-0260` | «Salva corriere e tracking» aggiorna soltanto l'unità autorizzata e rende subito visibile l'esito nell'archivio |
| `LEGACY-TEST-0364` | Le intestazioni italiane dell'export vengono rilevate esattamente e `DPD | 08448875901263` diventa corriere `DPD`, tracking `08448875901263` |
| `LEGACY-TEST-0365` | Corriere/tracking manuali sopravvivono a un sync API successivo che restituisce entrambi vuoti, nello stesso scope e sulla stessa unità |
| `LEGACY-TEST-0366` | Una riga importata con il solo numero ordine aggiorna tutte le unità dell'ordine e non produce falsi `unmatched` |

Nessuno di questi criteri chiude invio tracking al marketplace, scadenziario,
settlement, ticket, listini, connettori non implementati o parità della tabella completa.
