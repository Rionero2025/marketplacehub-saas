# B20.1 / B21.1 — Contratto degli ordini dalla versione Streamlit

Data dell'audit: 7 settembre 2026. Baseline SaaS: `22605323114c29b5161b4c9c4644910e8cf57434`.

Questo documento separa le regole effettivamente presenti nel programma originale dalle parti ancora da trasferire. Il blocco appartiene a **B20 Ordini Kaufland e cache** e **B21 Ordini multicanale**; B11 è la dashboard e non viene rinumerato. Nessun criterio del manifest diventa completato per effetto della sola lettura o della creazione di questo documento.

## 1. Fonti e differenze tra le copie

Fonti originali, lasciate in sola lettura:

- Locale: `C:/Users/Giorgio/Documents/marketplace_hub`.
- Checkout GitHub: `C:/Users/Giorgio/Documents/Codex/2026-09-04/leg/work/legacy-reference`, commit `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`.
- Specifica e criteri SaaS: `docs/reference/project-acceptance-index.json` e inventario dei blocchi già presente nel repository.

Il confronto è stato eseguito sul testo normalizzato UTF-8, neutralizzando soltanto le differenze di fine riga.

| File originale | Esito del confronto | Funzione nel contratto |
| --- | --- | --- |
| `pages/3_Ordini_Marketplace.py` — 58 righe | Identico | Selettore marketplace e apertura della pagina |
| `pages/3_Ordini_Kaufland.py` — 1.605 righe | Identico | Sincronizzazione, archivio, filtri, selezioni, riepiloghi, tracking e CSV |
| `services/kaufland_orders.py` — 1.417 righe | Identico | Download, normalizzazione, SKU, economia, tracking e scadenze |
| `services/kaufland.py` — 727 righe | Identico | Client firmato Kaufland |
| `services/worten.py` — 1.346 righe | Identico | Client Mirakl, importi e commissioni |
| `services/kaufland_order_costs.py` — 302 righe | Identico | Ricerca costo nei listini realmente pubblicati |
| `services/cecotec_orders.py` — 2.317 righe | Identico | Ordini K/W destinati ai fornitori, SKU e stati |
| `services/fx.py` — 57 righe | Identico | Cambio BCE e fallback |
| `services/order_selection.py` — 66 righe | Identico | Selezione coerente con i filtri |
| `services/accounting.py` | **Diverso**: locale 4.105 righe, GitHub 4.340 | La copia GitHub aggiunge recupero SKU dal payload e fallback di costo |
| `tests/test_kaufland_orders.py` | Presente nella copia locale, assente nel checkout GitHub | Esempi di regressione originali |

La differenza contabile è sostanziale: la copia locale risolve il costo dai cataloghi per EAN; la copia GitHub cerca **EAN → codice prodotto dello SKU nei cataloghi → costo incorporato nello SKU**. Inoltre recupera uno SKU composto ancora presente nel payload API quando il valore salvato è incompleto. Le funzioni di download Worten, quantità, importi, commissioni e netto non differiscono. Non si può dichiarare indistintamente che le due copie siano identiche.

## 2. Perimetro reale del programma originale

`pages/3_Ordini_Marketplace.py:25-48` considera soltanto gli account **Kaufland**: la pagina Ordini generica non ha già una seconda pagina Worten. I servizi Worten per gli ordini esistono invece in Contabilità e negli ordini ai fornitori. Una pagina SaaS unificata Kaufland/Worten è quindi l'estensione multicanale richiesta dall'utente, costruita sulle regole esistenti, non la copia di una schermata Worten già presente.

Il seller e l'account selezionati determinano ogni lettura e sincronizzazione. L'originale separa archivio `live` e `test`. Nel SaaS il perimetro deve comprendere organizzazione, seller, account e ambiente; una connessione rimossa o un'autorizzazione revocata non devono consentire ulteriori letture attraverso ID precedentemente conosciuti.

Non sono previsti, per questo pannello Seller, ripartizioni dell'utile tra gestore e partner: l'utente le ha escluse. Rimangono costo, commissione, netto e utile del singolo seller.

## 3. Kaufland: comando, download e salvataggio

Fonti: pagina `3_Ordini_Kaufland.py:85-205`; servizio `kaufland_orders.py:130-195`, `1052-1242`.

1. Scelta dell'account Kaufland attivo del seller.
2. Ambiente Playground disattivato per impostazione iniziale.
3. Quantità di download: **ultimi 500**, **ultimi 1.000** (predefinito), **ultimi 5.000**, **tutti disponibili**.
4. Opzione **Verifica dettagli API degli ordini spediti**, inizialmente attiva.
5. Sincronizzazione esplicita con progresso, riepilogo dell'ultima esecuzione e conteggi salvati/dettagli verificati.

### 3.1 Paginazione e identità

L'oggetto è una **unità fisica d'ordine**. Due pezzi possono avere lo stesso `id_order` e due `id_order_unit` diversi; il numero di righe non è il numero di ordini commerciali distinti.

Il download originale compie otto passaggi, in quest'ordine:

`cancelled`, `need_to_be_sent`, `open`, `received`, `returned`, `returned_paid`, `sent`, `sent_and_autopaid`.

Ogni passaggio usa `GET /v2/order-units`, con `status`, `limit` massimo 100 e `offset`. Un limite richiesto di N è applicato prima a ciascuno stato, poi al risultato globale. Le unità vengono deduplicate per `id_order_unit`, ordinate in modo decrescente per data creazione, data aggiornamento e ID, e solo allora limitate globalmente. Scaricare soltanto N righe dal primo stato non è equivalente.

L'originale interrompe la pagina vuota, corta oppure arrivata a `pagination.total`. In assenza del totale, il fallback storico può arrestarsi alla prima pagina: il connettore SaaS deve gestire la mancanza del totale in modo esplicito, senza proclamare completo un archivio troncato. La riga senza ID stabile deve produrre un errore riconoscibile, non un ordine artificiale.

Gli endpoint e l'enum coincidono con la specifica ufficiale corrente: limite massimo 100, default 30, ordinamento default per creazione decrescente e filtri temporali per creazione/aggiornamento. [OpenAPI Kaufland](https://sellerapi.kaufland.com/swagger.json).

### 3.2 Dettagli e conservazione

Per stati `sent`, `sent_and_autopaid`, `received`, `returned`, `returned_paid`, la sincronizzazione con dettagli attivi legge `GET /order-units/{id}`. Preferisce i campi non vuoti del dettaglio e integra il prodotto con i dati dell'elenco. Se manca ancora il tracking, legge `GET /orders/{id_order}`, riutilizzato per le unità dello stesso ordine, e cerca ricorsivamente l'unità corrispondente.

Un errore del dettaglio non cancella la riga già ricevuta nell'elenco. L'originale salva la riga e registra l'errore parziale. L'esecuzione distingue `completed`, `completed_with_errors` e `failed`; non mostra un successo pieno se i dettagli sono mancanti.

La chiave dell'archivio è account/ambiente/unità; il seller deve sempre coincidere. L'upsert aggiorna gli importi e lo stato, conserva il payload originale e non svuota tracking, corriere o data di verifica precedenti quando l'API restituisce valori mancanti. Non è una sincronizzazione incrementale con checkpoint: il comando originale rilegge l'intero perimetro selezionato.

## 4. Kaufland: campi ed economia esatta

Fonti: `kaufland_orders.py:839-959`, `970-1050`, `1347-1382`; spiegazione della pagina originale a `1568-1604`.

| Campo | Origine e significato |
| --- | --- |
| Ordine / unità | `id_order` / `id_order_unit` |
| Data | `ts_created_iso`; aggiornamento separato `ts_updated_iso` |
| SKU | `id_offer` senza ricostruire uno SKU fittizio |
| EAN | Primo valore presente in `product.eans` oppure `product.ean`, anche se array o dizionario |
| Nome prodotto | `product.title` |
| Marketplace nazionale | `storefront`; default valuta EUR per DE/AT/FR/IT/SK, PLN per PL, CZK per CZ |
| Stato | Codice API conservato, etichetta italiana separata |
| Prezzo / spedizione | `price / 100`, `shipping_rate / 100` |
| Ricavo lordo / netto API | `revenue_gross / 100`, `revenue_net / 100`, conservati distintamente |

Gli importi monetari Kaufland sono espressi in **unità minime**. `10000` significa `100,00` nella valuta della riga, non 10.000 euro.

Formule:

- Totale venduto = prezzo prodotto + spedizione.
- Commissione: preferire il campo esplicito `commission`, `commission_amount`, `commission_gross`, `marketplace_fee`, `marketplace_commission`; stessa ricerca dentro `fees`, comprese le voci della lista che identificano una commissione. I valori della commissione vengono resi positivi.
- Se il campo esplicito manca e prezzo e `revenue_gross` esistono: commissione = `max(0, prezzo - revenue_gross)`.
- Commissione percentuale = commissione / totale venduto × 100, a quattro decimali.
- Da ricevere = totale venduto − commissione; fallback storico, se possibile: `revenue_gross + spedizione`.
- Lo sconto Kaufland è già incorporato nel prezzo restituito; non sottrarlo una seconda volta. L'API descrive inoltre le singole unità e le possibili temporanee assenze degli indirizzi durante lo stato aperto. [Documentazione ordini Kaufland](https://sellerapi.kaufland.com/?page=orders).

La riga cancellata mantiene gli importi ricevuti come evidenza. La pagina originale la esclude dai totali economici: non confondere importo restituito dall'API con importo da sommare.

### 4.1 SKU composto e costo Kaufland

Formato: `fornitore_codiceprodotto_costoacquisto_prezzominimo`, letto con **`rsplit("_", 3)`**. Il fornitore può contenere underscore. Il codice prodotto è opaco: può essere EAN, codice fornitore o altra stringa.

Il parser finanziario Kaufland richiede i due valori finali finiti e positivi. La virgola decimale è accettata. Costo e minimo incorporati sono in **EUR**; non vengono convertiti dalla valuta del marketplace. Una differenza tra EAN dell'ordine e codice presente nello SKU non invalida il costo.

Utile ordine = netto da ricevere in EUR − costo acquisto EUR. Percentuale utile = utile / costo × 100, **non utile / vendita**. L'originale usa arrotondamento Python `round` a due decimali; il porting conserva gli esempi e serializza importi canonici come stringhe decimali.

| SKU / netto EUR | Risultato originale |
| --- | --- |
| `Innpro_6974662350503_335.07_452.34`, netto 400 | Costo 335,07; minimo 452,34; utile 64,93; utile 19,38% |
| `AB_Online_8690842106835_44.80_58.24`, netto 52 | Fornitore `AB_Online`; costo 44,80; utile 7,20 |
| `AB-Online_8690842106835_44.80_58.24`, EAN ordine `111100000606`, netto 54,16 | Costo 44,80 mantenuto; utile 9,36; utile 20,89% |
| `in_C01030002_199_273`, netto 250 | Codice `C01030002`; costo 199; utile 51 |
| `ceco_A01_EU01_110320_249_340` | Fornitore `ceco_A01_EU01`; codice `110320`; costo 249 |
| `SKU-ORIGINALE` oppure `force_KLCS14SAKHPCG` | Nessun costo inventato |

### 4.2 Fallback listini ancora da trasferire

La pagina Ordini K usa **prima il costo SKU**. Soltanto se manca, chiama `kaufland_order_costs.resolve_published_catalog_purchase_cost`.

Il catalogo di ricerca non è un listino generico: deriva dagli invii riusciti CREA/AGGIORNA per seller/account/ambiente, comprese pubblicazioni storiche. Individua le viste/listini effettivamente usati, recupera EAN/SKU originali della pubblicazione, preferisce la vista di origine e tenta EAN prima di SKU/codice. Il prodotto deve fornire un costo positivo. La fonte e il riferimento del listino restano visibili.

Il primo blocco SaaS conserva costo SKU e avviso esplicito quando servirebbe il fallback. **Non copre ancora la parità della ricerca nei listini pubblicati** e non deve presentarla come implementata.

## 5. Filtri, selezione, pagamenti e funzioni della pagina originale

Fonti: `3_Ordini_Kaufland.py:596-1069`, `1200-1565`; `kaufland_orders.py:278-564`.

La pagina originale offre filtri per intervallo di date, stati, nazioni, ricerca testuale, disponibilità pagamento, presenza tracking, presenza commissione, valuta originale, corriere e intervallo del venduto EUR. La ricerca è letterale, senza regex, e comprende ordine, unità, SKU, EAN, nome, tracking e corriere. Stati e nazioni sono inizialmente tutti selezionati; deselezionarli tutti produce nessun risultato.

Discrepanza concreta del sorgente: il filtro data usa il giorno **UTC**, mentre la colonna Data converte a **Europe/Rome**. Il payload canonico mantiene l'istante UTC; un eventuale allineamento dei filtri alla giornata italiana va dichiarato, non spacciato per comportamento già identico.

La tabella include selezione, nazione, data, ordine, unità, SKU, EAN, nome, prezzo prodotto, spedizione, venduto, commissione, percentuale e fonte, da ricevere, costo, utile, percentuale utile, metodo/fonte/riferimento costo, EUR e valuta originale, stato, spedito/ricevuto, data e stato pagamento, regola, ritardo e ID ticket, fonti delle date, corriere e tracking.

La selezione è legata alla firma di seller/account/ambiente/filtri. Ogni nuova firma inizializza tutte le sue righe filtrate; tornando a una firma già visitata recupera la scelta salvata e la interseca con le righe ancora visibili. Non eredita la selezione di un filtro diverso. Riepiloghi e CSV lavorano sul blocco filtrato o sulla selezione corrente, senza includere righe nascoste.

Le ulteriori funzioni sono parti vere della pagina, non dettagli estetici:

- Import tracking da CSV/XLSX Seller Portal, riconoscimento/mappatura colonne e applicazione alle unità dell'ordine.
- Inserimento manuale di corriere/tracking, conservato durante sincronizzazioni successive.
- Tabella dedicata al pagamento e selezione autonoma; riepilogo economico del solo blocco scelto.
- Con tracking: stima consegna + 14 giorni; senza tracking: spedizione + 21 giorni. Se manca la data necessaria, pagamento non determinabile.
- Le durate dei ticket posticipano la stima; un ticket aperto rende la data provvisoria. Il riepilogo segnala le date mancanti, usa l'ultima data del blocco e distingue disponibile/in attesa.
- Data effettiva di rilascio API prioritaria. Per `sent_and_autopaid`, `ts_updated_iso` può rappresentare il passaggio allo stato pagato. **Mai usare un generico aggiornamento come consegna**.
- Due CSV UTF-8 con BOM: ordini selezionati nel filtro e intero blocco filtrato, con gli stessi valori mostrati.

Il normalizzatore del primo blocco trasferisce tracking e date effettive con relativa fonte. **Non trasferisce ancora scadenziario, ticket, modifica/import tracking, selezione dei pagamenti o CSV**. Conservare una data non equivale a completare questi flussi.

## 6. Worten: sorgente appropriata e differenze monetarie

Fonti: `services/worten.py:235-405`; `services/accounting.py` locale `1104-1480`, checkout `1251-1631`; `services/cecotec_orders.py:1514-1617`.

`worten.list_orders()` richiede un insieme di `offer_ids` e serve a trovare commissioni per offerte: **non è il download generale degli ordini del negozio**.

La fonte generale è `fetch_worten_accounting_orders` / `fetch_worten_orders`: Mirakl OR11 `GET https://marketplace.worten.pt/api/orders`, header `Authorization` contenente direttamente la chiave API, `shop_id`, `max=100`, `offset`. I filtri completi usano `start_date`/`end_date`; la variante incrementale contabile usa `start_update_date`/`end_update_date`. Il client ufficiale identifica OR11 come recupero ordini e deserializza la collezione `orders`. [SDK ufficiale Mirakl](https://github.com/mirakl/sdk-php-shop/blob/master/src/Mirakl/MMP/Shop/Request/Order/Get/GetOrdersRequest.php).

La paginazione conta **ordini**, mentre il limite dell'importazione contabilizza le **righe ordine**. Ogni ordine viene appiattito dalle sue `order_lines`/`lines`. ID ordine: `order_id`, poi `commercial_id`/`id`; ID riga: `order_line_id`/`id`, con fallback storico `{ordine}-{indice+1}` quando necessario. Il connettore deve rendere tale scelta deterministica prima della normalizzazione.

Una riga Mirakl può avere quantità maggiore di uno. Non creare molteplici righe fittizie e non moltiplicare due volte il prezzo.

| Informazione | Regola originale |
| --- | --- |
| SKU | `offer_sku`, `seller_sku`, `shop_sku`, `offer_id`, `sku`, `product_sku`; checkout GitHub recupera uno SKU composto effettivamente presente nel payload |
| Titolo | `product_title`, `product_name`, `title`, `product_label` |
| Data / stato | Data dell'ordine; stato riga prioritario rispetto allo stato ordine |
| Quantità | Intero almeno 1 |
| Vendita | Prima `total_price`/totale riga; altrimenti `unit_price` × quantità + spedizione; altrimenti `price` + spedizione, senza moltiplicazione |
| Commissione | `abs(total_commission)` prioritario; altrimenti `abs(commission_fee) + abs(commission_vat)` |
| Percentuale commissione | Percentuale esplicita API; altrimenti fee **senza IVA** / base commissionabile × 100. Preferire le parti con `commissionable=true` nei breakdown |
| Netto | Campo esplicito di payout prioritario; altrimenti vendita netta meno commissione |
| Valuta | Riga, poi ordine, default EUR del servizio originale |
| Costo | Costo unitario del catalogo/fallback SKU × quantità |

Gli importi Mirakl sono già nella **valuta principale**: `100` significa `100,00`, non `1,00`. `price` è normalmente già l'importo di riga. Soltanto un campo `unit_price` dimostra la necessità di moltiplicare.

Esempio: quantità 2, `price=100`, spedizione 5, commissione 10 più IVA 2,30, costo SKU 20. Venduto 105; commissione 12,30; netto 92,70; costo totale 40; utile 52,70. Il tasso commissionale dedotto dalla fee è 10/105, distinto dalla percentuale della commissione comprensiva d'IVA.

### 6.1 Annullamenti e rimborsi Worten

La regola originale `_zero_economics_reason` azzera venduto, rimborso, commissione, costo e netto per cancellato/rifiutato/no-stock, e anche per `REFUNDED`, `FULLY_REFUNDED`, **`PARTIALLY_REFUNDED`**. È un comportamento contabile effettivamente presente, anche se il nome dello stato suggerisce un rimborso parziale. Il porting lo identifica come **regola originale per stato**, conservando il payload API distinto.

Per un rimborso esplicito in altri stati, il rimborso è limitato al venduto e sottratto alla vendita. In uno stato di reso senza importo rimborso, l'originale presume il rimborso integrale. Un `RETURNED` può quindi mantenere il costo e produrre utile negativo. Il payout esplicito, quando presente e quando non opera l'azzeramento per stato, mantiene priorità sul calcolo.

Deviazione deliberata richiesta dalla rappresentazione dei dati mancanti: l'originale può trattare una commissione mancante come zero per ottenere il netto. Il SaaS usa **netto esplicito API oppure `None` con avviso** quando non può calcolarlo. Una commissione non nota non diventa una commissione gratuita.

### 6.2 Costo Worten e limite del primo blocco

La variante GitHub dà priorità ai cataloghi e solo infine al costo incorporato. Finché i cataloghi non sono trasferiti, il SaaS mostra il costo SKU come fonte esplicita e segnala il confronto prioritario con i listini ancora indisponibile.

Il parser Worten/fornitori considera strutturalmente valido lo SKU con quattro componenti e fornitore/codice presenti anche se il minimo non è numerico; per il fallback finanziario basta un costo positivo. Questa differenza rispetto al parser finanziario Kaufland è conservata. Il codice prodotto non numerico resta codice SKU e non viene etichettato come EAN.

Il recupero SaaS cerca le chiavi SKU della **riga corrente**. Non cerca nelle righe sorelle dell'ordine, per evitare di attribuire il costo di un altro prodotto. Non è dichiarata parità completa del recupero ricorsivo di tutti i vecchi payload contabili.

## 7. Contratto canonico del normalizzatore SaaS

File: `packages/core/src/marketplace_hub_core/orders/normalization.py`.

```python
normalize_order_line(marketplace, raw, *, fx_rates=None) -> dict
merge_order_unit(base, detail) -> dict
find_order_unit(payload, unit_id) -> dict
extract_tracking(payload) -> tuple[str, str]  # corriere, tracking
```

Per Kaufland `raw` è l'unità risultante dall'integrazione elenco/dettaglio. Per Worten è la riga con il contesto dell'ordine in `raw['_order']`. L'eventuale snapshot `raw['_fx']` contiene `rates`, `date`, `source`, `online`. `_detail_warning` ammette soltanto il codice sicuro `details_unavailable`.

Il risultato contiene:

- Identità: `external_line_id`, `order_id`, `marketplace`; ID interno assegnato dal repository.
- Dati: `created_at` ISO UTC o `None`, `status`, `status_label`, `storefront`, `currency`, `product_name`, `ean`, `sku`, `quantity`.
- Importi nella valuta originale: `sale_amount`, `shipping_amount`, `commission_amount`, `commission_rate`, `payout_amount`.
- Conversioni esplicite: `sale_amount_eur`, `shipping_amount_eur`, `commission_amount_eur`, `payout_amount_eur`.
- Economia in EUR: `purchase_cost_eur`, `profit_amount_eur`, `profit_pct`, `purchase_cost_source`; alias `purchase_cost` e `profit_amount`, sempre identificati come EUR nei dettagli.
- `monetary_warnings`, `details`, payload `raw` **privato** per persistenza; il DTO pubblico non deve esporre `raw`.

`details` conserva prezzo prodotto originale/EUR, fonti commissione/netto, quantità economiche accessorie, supplier/codice/minimo/costo unitario SKU, tracking/corriere, date effettive e relative fonti, FX e `excluded_from_totals`. Per Worten include venduto iniziale, rimborso e regola economica. Non contiene header, credenziali o intere risposte remote.

Il DTO usa stringhe decimali per gli importi, `None` per valori ignoti e avvisi comprensibili. Vengono respinti booleani monetari, NaN e infiniti. Manca un ID stabile: `ValueError('missing_order_identifier')`. Marketplace non supportato: `ValueError('unsupported_marketplace')`. Non si producono righe fittizie per mascherare errori di formato.

### 7.1 Cambi EUR

Formula originale BCE: valore EUR = valore originale / tasso espresso come unità della valuta per 1 EUR. Si arrotonda a due decimali. Il costo SKU è già EUR.

L'originale usa cambio giornaliero BCE, cache e persino valori bootstrap fissi se manca qualsiasi cache. Il primo blocco SaaS **non introduce un bootstrap arbitrario**: conserva l'importo originale; se il cambio manca, la conversione e l'utile che ne dipende restano `None`. Mostrare origine e data del tasso disponibile. Il fallback offline originale non è dichiarato trasferito.

## 8. Verifica e manifest: cosa è candidabile

`tests/test_order_normalization.py` comprende 38 casi passati: esempi SKU originali, minor unit Kaufland/major unit Worten, quantità, importi mancanti, commissione esplicita e IVA, FX, stati e rimborsi, date reali, tracking annidato, input immutato, SKU Worten e recupero della sola riga corrente. Il file originale non è stato modificato. Questa verifica riguarda il normalizzatore; non dimostra da sola il funzionamento di API, worker, permessi, dashboard o deploy.

| Gruppo di criteri del manifest | Evidenza possibile nel blocco | Limite da rispettare |
| --- | --- | --- |
| `LEGACY-TEST-0339`–`0347` | Formule K e nove regole/esempi SKU, fixture normalizzatore | Nessuna conclusione sul fallback listini |
| `LEGACY-TEST-0360`, `0362`, `0363`, `0367`, `0368`, `0369` | Commissione prioritaria, tracking, unità nei dettagli, valuta | Verificare anche persistenza/DTO quando richiesto |
| `LEGACY-TEST-0349`, `0350`, `0351`, `0354` | Date API e priorità del rilascio | Alcuni criteri includono la funzione di pagamento completa: non assegnarli interamente solo per conservazione timestamp |
| `LEGACY-TEST-0337`, `0338`, `0376`, `0377` | Richiedono connettore e test di paginazione/salvataggio/dettagli | Non coperti dal solo normalizzatore |
| `LEGACY-UI-0252`, `0261`, `0264` | Scelta account, ricerca, scelta marketplace | Richiedono verifica del flusso UI reale |
| `LEGACY-UI-0262` | Tabella originale | Non chiudere per una tabella ridotta senza campi/filtri/selezione richiesti |
| `MASTER-0313`–`0317`, `0320`–`0328`, `0330` | Dati normalizzati e scope possono fornire evidenza | Valutare singolarmente visibilità, filtri e API; no completamento automatico |

Rimangono distintamente pendenti: `LEGACY-UI-0253` se Playground non disponibile; `0254`–`0260` import/modifica tracking; `0263` tabella pagamenti; `LEGACY-TEST-0348`, `0352`, `0353`, `0355`–`0359`, `0361`, `0364`–`0366`, `0370`–`0375` per le relative funzioni complete. Sono inoltre da completare i dati cliente, stato ordine fornitore, stato contabile, filtri fornitore/verifica contabile, listini, esportazioni e collegamenti tra moduli richiesti da `MASTER-0318`, `0331`, `0332`, `0338`, `0340`.

Non usare un numero di test verdi come percentuale del progetto. Il completamento finale è quello dei criteri verificati nel manifest condiviso; nessuno stato del manifest è stato modificato da questo audit/porting del normalizzatore.
