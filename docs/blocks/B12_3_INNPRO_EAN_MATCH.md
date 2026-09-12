# B12.3 — Match EAN InnPro sulle viste già salvate

## Ambito

Il match LIGHT/FULL già presente nella preparazione di nuove viste è ora disponibile anche sulle viste salvate attraverso «Abbina EAN e completa dati». Sorgenti InnPro dello stesso fornitore e Seller, versioni esplicite, EAN esatto univoco. Nessun abbinamento per nome, SKU o rimozione degli zeri iniziali.

LIGHT aggiorna la quantità della vista. FULL completa nomi e misure mancanti e aggiunge marca, categoria, descrizione testuale, codice standard del produttore e garanzia se presenti. Il comando conserva prezzi, costi, SKU, nomi e misure già impostati. Le dimensioni senza unità riconosciuta restano indisponibili; non viene supposta un’unità. La quantità corrisponde alla versione LIGHT importata, non a una lettura istantanea del magazzino remoto.

Riepilogo EAN FULL/LIGHT abbinati, mancanti, duplicati. Nessuna scelta arbitraria fra duplicati. Le righe con stock LIGHT non verificabile sono bloccate nell’anteprima di pubblicazione, salvo quantità inserita esplicitamente dall’operatore. Dati e stato match persistono nella vista e passano alle nuove anteprime di pubblicazione. I listini originali e le anteprime già create non vengono riscritti; la nuova revisione vista invalida le vecchie bozze di invio.

## Implementazione

Lettura in batch da 100 righe, conteggi EAN lato database e lettura dei soli FULL univoci; metadati canonici limitati a 128 Ki caratteri per riga. Proiezione consentita dei soli campi descrittivi. Nessun caricamento dell’intero XML o sorgente privata nel browser. Lock delle sorgenti/versioni e della vista; revisione obbligatoria contro aggiornamenti concorrenti. API e proxy applicano isolamento Seller e permesso catalogo prima del body. Nessuna migrazione database.

## Verifiche

Test nuovi: completamento vista e passaggio al payload pubblico, stock solo LIGHT (FULL con stock/costi volutamente differenti), conservazione prezzi e ID, nomi/pesi manuali conservati, dimensioni, duplicati e mancanti, revisioni, isolamento Seller e permessi. Collaudo online riportato di seguito.


## Collaudo staging — 12 settembre 2026

Commit `89a7798db23711722b8206bea8da654e40b761ff` Live su web, API e worker. Deploy web `dep-daio3n0ae00c73djcis0`, API `dep-daio3n0ae00c73djcj0g`, worker `dep-daio3n0ae00c73djcja0`. Health API e web HTTP 200.

Eseguito match autenticato sulla vista esistente Innpro di RioneroShop: 3363 righe, FULL 3337 EAN univoci, 25 mancanti, 1 duplicato. LIGHT: tutti i 3363 EAN univoci e stock verificato. Prima riga EAN 6930460000040: nome Charger SkyRC iMax B6AC V2, peso 1,03 kg, costo 37,93 EUR, spedizione 15 EUR, totale 52,93 EUR, quantità 258, vendita 71,46 EUR, minimo 58,22 EUR. Nomi della seconda e terza riga: SkyRC iMax B6 Mini Charger; Charger LiPo SkyRC E3. Dettagli FULL: marca SkyRC, categoria RC models/Charging/Chargers, descrizione testuale, codice produttore e garanzia. Dimensioni della prima riga non disponibili, mostrate senza attribuire valori o unità non verificati.

28 test backend (25 regressioni catalogo/pubblicazione + 3 nuovi) superati; 170 test web esistenti e un nuovo controllo DTO, 171 complessivi; typecheck e build Next riusciti.

Nuova anteprima di pubblicazione creata alle 18:51:34 Europe/Rome per le righe 1–3: verificati i tre nomi, dettagli FULL e stock LIGHT verificato. Alla fine tutte le righe sono deselezionate, conferma PUBBLICA vuota e invio disabilitato. Nessuna offerta trasmessa al marketplace.

## Avanzamento certificato

Chiuso il solo criterio MASTER-0228 («feed full/light;») con le sorgenti importate operative e il collaudo autenticato del loro uso congiunto. Gli altri criteri InnPro, inclusi immagini/documenti/parametri completi, non sono dichiarati conclusi. Totale 165/2011 = 8,20%.
