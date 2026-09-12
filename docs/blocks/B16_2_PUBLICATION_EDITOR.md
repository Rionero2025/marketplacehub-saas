# B16.2 — Editor anteprima e selezione pubblicazione

## Modifiche

Tabella con selezione delle singole righe, seleziona tutti e deseleziona tutti sulle righe dell’intervallo caricato. Estremi Da/A inclusivi nell’elenco filtrato, massimo 100 righe per invio, limite mostrato esplicitamente. Nome, EAN, SKU, quantità, peso, costo totale, vendita, minimo e commissione stimata modificabili; guadagno derivato al salvataggio. Supporto virgola decimale senza separatore delle migliaia.

Modifiche salvate nella sola anteprima: vista e listino originali restano invariati. I payload Kaufland e Worten usano i prezzi manuali salvati senza ricalcolo del ricarico. SKU composto aggiornato dopo modifiche a EAN/costo/minimo; un override SKU esplicito resta manuale anche nei salvataggi successivi. Valori invalidi e SKU duplicati impediscono la selezione delle righe interessate.

Salvataggio e conferma richiedono la versione SHA-256 dello snapshot letto. Il controllo sotto lock dell’invio impedisce sovrascritture e invio di valori cambiati da un’altra sessione. Le modifiche sono ammesse solo su bozze; dopo conferma, le righe restano immutabili. Permesso catalogo e isolamento Seller controllati prima di leggere il body. Nessuna nuova migrazione database.

## Validazione prima del rilascio

17 test backend pubblicazione superati, inclusi prezzi manuali fino al payload esterno simulato, selezione parziale, originali invariati, concorrenza, duplicati, isolamento Seller, numeri invalidi, CSV Worten e intervallo 2–3. 7 test frontend/proxy superati. Typecheck e build Next.js superati. Invii reali esclusi dal collaudo.

## Stato

Implementazione pronta per il rilascio e verifica autenticata. Copertura complessiva ancora 163/2011 (8,11%); nessun criterio promosso prima del collaudo visibile.
