# B20.4 — Scadenziario pagamenti e ritardi ticket degli ordini

Fonti originali lette in sola lettura:

- `C:/Users/Giorgio/Documents/marketplace_hub/services/kaufland_orders.py`, funzioni
  `_received_timestamp`, `_shipped_timestamp`, `_payment_release_timestamp`,
  `payment_schedule`, `ticket_holds`, `selected_payment_deadline` e
  `selected_order_financial_summary`;
- `C:/Users/Giorgio/Documents/marketplace_hub/pages/3_Ordini_Kaufland.py`, righe
  202–297, 683–693, 778–794 e 1197–1477;
- `C:/Users/Giorgio/Documents/marketplace_hub/tests/test_kaufland_orders.py`, righe
  313–575, 593–610 e 753–868.

Il contratto trasferisce nel pannello Seller Enterprise la stessa logica con cui il
programma Streamlit calcola e presenta la data prevista di disponibilità del netto.
La regola è specifica del canale: questo blocco può applicarla a **Kaufland** perché le
fonti originali disponibili descrivono Kaufland. Un altro marketplace deve avere un
proprio adattatore verificato; non può ereditare automaticamente i termini Kaufland.

La data calcolata è una previsione operativa. La data effettiva comunicata dall'API ha
priorità e il booking report del marketplace resta la conferma contabile definitiva.
Questo blocco non effettua bonifici, non riconcilia settlement e non sostituisce il
riscontro finanziario del marketplace.

## Eventi temporali ammessi

Le tre date usate dallo scadenziario hanno significati distinti. Il primo valore ISO
valido, nell'ordine indicato, viene normalizzato in UTC e conserva la propria fonte.

| Evento | Campi originali, in ordine di priorità | Fallback ammesso |
| --- | --- | --- |
| Consegna/ricezione | `order_received_timestamp_iso`, `ts_received_iso`, `received_at`, `received_at_iso`, `ts_delivered_iso`, `delivered_at`, `delivery_date` | Nessuno: `ts_updated_iso` generico non è una consegna |
| Spedizione | `order_sent_timestamp_iso`, `ts_sent_iso`, `sent_at_iso`, `sent_at`, `ts_shipped_iso`, `shipped_at_iso`, `shipped_at` | Solo nello stato `sent`, `ts_updated_iso` rappresenta il passaggio a spedito |
| Rilascio del netto | `revenue_released_timestamp_iso`, `revenue_released_at`, `payout_timestamp_iso`, `payout_at`, `payment_timestamp_iso`, `paid_at_iso`, `paid_at` | Solo nello stato `sent_and_autopaid`, `ts_updated_iso` rappresenta il rilascio |

Gli eventi di ricezione e spedizione sono accettati soltanto per gli stati
`sent`, `sent_and_autopaid`, `received`, `returned` e `returned_paid`. Una data di
aggiornamento generica non può diventare contemporaneamente consegna e pagamento.
Questa separazione impedisce di posticipare erroneamente di 14 giorni un accredito già
rilasciato.

Le righe archiviate con il vecchio fallback «aggiornamento allo stato Ricevuto» vengono
riesaminate dal payload API originale conservato. Se il payload contiene una vera data
di consegna, questa sostituisce il fallback e la previsione viene ricalcolata. Se non la
contiene, la data e la previsione insicure vengono eliminate. La compatibilità deve
funzionare anche durante un rilascio progressivo, quando una riga esistente non possiede
ancora tutte le nuove proiezioni SQL.

## Matrice dello scadenziario

| Condizione della riga | Data usata o stimata | Disponibilità e stato | Definitività |
| --- | --- | --- | --- |
| Data effettiva di rilascio presente | Timestamp effettivo, senza applicare stime o ritardi ticket | Disponibile se la sua data UTC è oggi/passata; nello stato `sent_and_autopaid` è disponibile anche se il timestamp è futuro | `payment_date_final = true` |
| `sent_and_autopaid` senza data effettiva | Nessuna data inventata | «Ricavato già disponibile · data non disponibile» | Disponibile, ma data non definitiva |
| Tracking presente e consegna rilevata | Consegna + 14 giorni | Conto alla rovescia fino alla data prevista | Definitiva in assenza di ticket aperti |
| Tracking presente e consegna assente | Nessuna data | «Tracking presente · consegna non ancora rilevata» | Non definitiva |
| Tracking assente, stato spedito e data di spedizione presente | Spedizione + 21 giorni | Conto alla rovescia fino alla data prevista | Definitiva in assenza di ticket aperti |
| Tracking assente, stato spedito e data di spedizione assente | Nessuna data | «Data di spedizione non disponibile» | Non definitiva |
| Ordine non ancora spedito | Nessuna data | «Non ancora spedito» | Non definitiva |
| Stima con soli ticket chiusi | Alla stima base viene aggiunta la durata complessiva dei ticket | Il conteggio riflette la data posticipata | Definitiva |
| Stima con almeno un ticket aperto | Alla stima base viene aggiunta la durata maturata fino all'istante corrente | «Ticket aperto · data in aggiornamento»; il netto resta non disponibile | Non definitiva e cresce finché il ticket resta aperto |

Il conto alla rovescia confronta **date di calendario UTC**. Una scadenza futura mostra
«Tra N giorni», una scadenza nella data corrente «Disponibile oggi» e una scadenza
passata «Disponibile da N giorni». Per una data effettiva le etichette equivalenti
iniziano con «Disponibile». L'ora serve a conservare l'evento esatto, ma non introduce
frazioni nel numero di giorni.

`ticket_delay_days` è la durata totale in secondi divisa per 86.400 e arrotondata a due
decimali. Un valore negativo ricevuto dal chiamante viene ricondotto a zero. Se esiste
una data effettiva di rilascio, questa prevale anche sui ticket e il ritardo esposto per
quella riga torna a zero.

## Aggregazione dei ticket

I ticket vengono raggruppati per ID unità ordine. Un ticket chiuso contribuisce
dall'istante di creazione all'ultimo aggiornamento; un ticket con stato esatto
`opened` contribuisce dalla creazione all'istante corrente. Intervalli privi di data,
invertiti o non associati a un'unità vengono ignorati.

Gli intervalli sovrapposti o contigui della stessa unità vengono uniti prima di sommare
la durata, così due ticket contemporanei non raddoppiano il rinvio. Il riepilogo conserva
comunque il numero di ticket validi, quanti sono aperti e gli ID distinti nel loro ordine.
Un ticket collegato a più unità contribuisce separatamente a ognuna.

Nel SaaS la sincronizzazione ordini Kaufland tenta anche di acquisire uno snapshot
completo dei ticket nei cinque stati originali:

- `opened`;
- `buyer_closed`;
- `seller_closed`;
- `both_closed`;
- `customer_service_closed_final`.

Ogni stato è paginato a blocchi di 30 elementi. Gli ID ticket vengono deduplicati e una
pagina ripetuta o un elemento senza ID rende lo snapshot non affidabile. Il mancato
aggiornamento dei ticket non deve impedire il salvataggio degli ordini: il job termina
con un avviso e conserva l'ultimo snapshot valido. Uno snapshot completo successivo
sostituisce atomicamente quello precedente soltanto nello stesso scope.

## Filtri e tabella Seller

Accanto ai filtri già disponibili, la pagina Ordini espone «Stato pagamento» con queste
scelte funzionali:

| Scelta | Righe incluse |
| --- | --- |
| Tutti | Nessun vincolo aggiuntivo sul pagamento |
| Netto disponibile | Righe con `payment_available = true` |
| In attesa di accredito | Righe non disponibili e con una data prevista |
| Data non determinabile | Righe senza data prevista, anche quando `sent_and_autopaid` rende già disponibile il netto o la riga è cancellata |
| Ticket aperto | Righe con almeno un ticket aperto |

Queste quattro espressioni riproducono esattamente i filtri Streamlit. L'esclusione
delle cancellazioni avviene nel riepilogo delle righe selezionate, non viene aggiunta
silenziosamente ai filtri dell'archivio.

La sezione «Date previste di pagamento» mantiene una selezione esplicita per riga e
mostra almeno:

- ordine, unità ordine, nazione e stato ordine;
- presenza del tracking, data di spedizione e data di ricezione;
- data di pagamento/disponibilità e giorni al pagamento;
- netto da pagare, costo acquisto e guadagno ordine;
- metodo e fonte del costo;
- stato, regola e fonte del pagamento;
- ritardo ticket, numero ticket aperti e ID ticket.

Una riga cancellata è identificabile come tale, vale zero nei dati di pagamento e non
entra nella scadenza del blocco. Le date vengono conservate in UTC e presentate in ora
italiana; il calcolo dei giorni resta quello UTC della funzione originale.

La selezione deve operare sulle identità persistenti dell'archivio, sopravvivere alla
paginazione e considerare soltanto lo scope filtrato corrente. La sezione pagamenti ha
una seconda selezione indipendente, inizialmente vuota, la cui popolazione eleggibile è
esattamente l'intersezione con la selezione principale degli ordini. «Seleziona tutte»
e «Deseleziona tutte» modificano la rispettiva selezione senza caricare tutto lo storico
nel browser; togliere una riga dalla selezione principale la elimina anche dal riepilogo
pagamenti.

## Riepilogo delle righe selezionate

`selected_order_financial_summary` considera esclusivamente le righe spuntate. Espone:

- righe scelte, righe pagabili e righe cancellate;
- netto complessivo, costo conosciuto e guadagno conosciuto;
- righe con costo conosciuto o non calcolabile;
- righe e netto già disponibili;
- righe e netto ancora in attesa.

Le righe cancellate non contribuiscono a nessun importo. Una riga attiva con costo o
profitto assente contribuisce al netto, ma incrementa `unknown_cost_units` e non viene
inclusa nei totali di costo e guadagno. Il pannello deve segnalarlo esplicitamente,
senza trasformare il valore ignoto in un falso costo zero.

`selected_payment_deadline` esclude le righe cancellate e usa la data definitiva più
tarda delle righe pagabili. Una data provvisoria, compresa quella con ticket aperto, è
classificata come non determinabile. Il risultato distingue righe programmate e senza
data, restituisce gli identificativi senza scadenza e dichiara «tutte disponibili» solo
quando ogni riga pagabile è disponibile. Un blocco formato soltanto da cancellazioni
non possiede una data di pagamento.

## Persistenza e ricalcolo nell'architettura SaaS

Lo snapshot ticket è isolato per organizzazione, Seller, account marketplace e ambiente.
Lo stesso vincolo vale per righe ordine, filtri, selezione ed export. La sessione deve
autorizzare il Seller e il permesso `LOGISTICS`; account e ambiente vengono ricavati e
verificati dal backend, non accettati come scope arbitrario del browser.

Il dato canonico conserva eventi, fonte e campi descrittivi. Le proiezioni SQL indicizzano
data prevista, disponibilità, definitività, ticket aperto e ritardo ticket per rendere
server-side filtri e aggregazioni. Il CSV aggiunge gli stessi campi visibili, senza
alterare valuta o precisione degli importi economici.

Disponibilità, giorni residui e durata dei ticket aperti dipendono dal tempo. Elenco,
dettaglio ed export arricchiscono in memoria soltanto le righe già richieste, usando lo
snapshot ticket dello scope e l'istante UTC corrente; le richieste GET non riscrivono e
non rileggono l'intero archivio. I filtri e i riepiloghi SQL usano un'espressione di
disponibilità basata sulla data UTC corrente, così una scadenza passa a «disponibile»
anche senza una nuova sincronizzazione. La materializzazione completa avviene durante
la sincronizzazione ordini, la sostituzione dello snapshot ticket o la migrazione.

La migrazione deve:

1. aggiungere le proiezioni pagamento alle righe esistenti;
2. creare lo snapshot ticket con vincolo univoco per Seller/account/ambiente/ID ticket;
3. riparare e ricalcolare le righe pregresse a partire da dato canonico e payload API
   archiviato;
4. conservare un backup puntuale della presenza e del valore degli eventi riparati e
   permettere il downgrade eliminando soltanto campi e strutture del blocco, senza
   perdere l'ordine canonico precedente; un successivo upsert del provider annulla il
   backup obsoleto affinché il downgrade non ripristini eventi superati.

Per marketplace diversi da Kaufland, finché manca un adattatore verificato, il backend
deve dichiarare che il programma pagamenti non è disponibile e non deve applicare
silenziosamente le regole +14/+21.

## Stati ed errori osservabili

| Condizione | Risultato richiesto |
| --- | --- |
| Riga Kaufland con eventi sufficienti | Data, fonte, regola, countdown e disponibilità coerenti nella lista, nel dettaglio, nella selezione e nel CSV |
| Eventi insufficienti | Stato specifico e data vuota; nessuna data inventata |
| Ticket aperto | Data presentata come provvisoria, netto non disponibile e ritardo aggiornato nel tempo |
| Snapshot ticket non aggiornabile | Ordini sincronizzati; avviso «Ticket non aggiornati» e ultimo snapshot valido conservato |
| Marketplace senza regola verificata | «Programma pagamenti non disponibile per questo marketplace» |
| Sessione assente/scaduta | HTTP 401 senza esporre dati dello scope |
| Seller, permesso, account o ambiente non autorizzato | HTTP 403/404 coerente con i confini esistenti, senza rivelare righe di un altro tenant |
| Filtro o parametro pagamento non valido | HTTP 422, nessuna modifica ai dati |
| Errore interno | Risposta recuperabile con request ID; nessuna conferma di snapshot parziale |

## Ventuno criteri candidati, ancora da verificare

Il solo contratto non cambia il ledger. Tutti i criteri sotto restano `pending` finché
implementazione, test backend, test frontend e collaudo staging non dimostrano l'intero
comportamento richiesto.

| ID | Prova necessaria per la chiusura |
| --- | --- |
| `LEGACY-TEST-0348` | Una riga ricevuta con tracking usa consegna + 14 giorni e conserva gli importi economici attesi |
| `LEGACY-TEST-0349` | Il timestamp esplicito di ricezione non viene confuso con un successivo timestamp di rilascio/aggiornamento |
| `LEGACY-TEST-0350` | `sent_and_autopaid` usa il timestamp effettivo Kaufland, ne conserva la fonte e risulta disponibile |
| `LEGACY-TEST-0351` | Un `ts_updated_iso` generico nello stato ricevuto non diventa una falsa data di consegna |
| `LEGACY-TEST-0352` | Il vecchio fallback errato viene riparato dal payload API archiviato e la scadenza viene ricalcolata |
| `LEGACY-TEST-0353` | Il countdown distingue futuro, oggi e già disponibile usando le date UTC |
| `LEGACY-TEST-0354` | Un timestamp effettivo di rilascio prevale sulla stima |
| `LEGACY-TEST-0355` | Una riga spedita senza tracking usa spedizione + 21 giorni |
| `LEGACY-TEST-0356` | Una riga spedita con tracking attende una vera data di consegna |
| `LEGACY-TEST-0357` | La durata di un ticket chiuso posticipa la data e mantiene la stima definitiva |
| `LEGACY-TEST-0358` | Un ticket aperto rende la data provvisoria e il netto non disponibile |
| `LEGACY-TEST-0359` | Ticket aperti e chiusi producono durata, conteggi e associazione all'unità corretti senza doppio conteggio degli intervalli |
| `LEGACY-TEST-0361` | Una riga archiviata viene arricchita con commissione e stato pagamento anche senza le nuove colonne; la prova deve coprire l'intero criterio, non il solo scadenziario |
| `LEGACY-TEST-0370` | Il blocco selezionato usa l'ultima data definitiva e ignora le cancellazioni |
| `LEGACY-TEST-0371` | Il blocco segnala quantità e ID delle righe senza data di pagamento |
| `LEGACY-TEST-0372` | Un blocco `sent_and_autopaid` con data effettiva risulta interamente disponibile |
| `LEGACY-TEST-0373` | Il riepilogo economico considera soltanto le unità selezionate e separa disponibile da attesa |
| `LEGACY-TEST-0374` | Il riepilogo esclude gli importi cancellati e segnala il costo sconosciuto |
| `LEGACY-UI-0263` | La tabella Seller delle date previste espone selezione per riga, campi pagamento/ticket e riepilogo limitato alle righe scelte |
| `MASTER-0366` | Il SaaS rende operative e comprensibili le date previste di pagamento |
| `MASTER-0821` | La previsione di pagamento viene applicata soltanto quando prevista dalle regole verificate del canale |

Nessun criterio viene marcato da questo documento. Il ledger ufficiale resta a
**126/2.011 = 6,27%** fino al completamento e alla pubblicazione del blocco.
