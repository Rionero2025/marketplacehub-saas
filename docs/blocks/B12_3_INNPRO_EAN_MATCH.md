# B12.3 — Match EAN InnPro sulle viste già salvate

## Ambito

Il match LIGHT/FULL già presente nella preparazione di nuove viste è ora disponibile anche sulle viste salvate attraverso «Abbina EAN e completa dati». Sorgenti InnPro dello stesso fornitore e Seller, versioni esplicite, EAN esatto univoco. Nessun abbinamento per nome, SKU o rimozione degli zeri iniziali.

LIGHT aggiorna la quantità della vista. FULL completa nomi e misure mancanti e aggiunge marca, categoria, descrizione testuale, codice standard del produttore e garanzia se presenti. Il comando conserva prezzi, costi, SKU, nomi e misure già impostati. Le dimensioni senza unità riconosciuta restano indisponibili; non viene supposta un’unità. La quantità corrisponde alla versione LIGHT importata, non a una lettura istantanea del magazzino remoto.

Riepilogo EAN FULL/LIGHT abbinati, mancanti, duplicati. Nessuna scelta arbitraria fra duplicati. Le righe con stock LIGHT non verificabile sono bloccate nell’anteprima di pubblicazione, salvo quantità inserita esplicitamente dall’operatore. Dati e stato match persistono nella vista e passano alle nuove anteprime di pubblicazione. I listini originali e le anteprime già create non vengono riscritti; la nuova revisione vista invalida le vecchie bozze di invio.

## Implementazione

Lettura in batch da 100 righe, conteggi EAN lato database e lettura dei soli FULL univoci; metadati canonici limitati a 128 Ki caratteri per riga. Proiezione consentita dei soli campi descrittivi. Nessun caricamento dell’intero XML o sorgente privata nel browser. Lock delle sorgenti/versioni e della vista; revisione obbligatoria contro aggiornamenti concorrenti. API e proxy applicano isolamento Seller e permesso catalogo prima del body. Nessuna migrazione database.

## Verifiche

Test nuovi: completamento vista e passaggio al payload pubblico, stock solo LIGHT (FULL con stock/costi volutamente differenti), conservazione prezzi e ID, nomi/pesi manuali conservati, dimensioni, duplicati e mancanti, revisioni, isolamento Seller e permessi. Collaudo online da registrare dopo il rilascio.
