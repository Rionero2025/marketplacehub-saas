# B16.2 — Editor anteprima e selezione pubblicazione

## Modifiche

Tabella con selezione delle singole righe, seleziona tutti e deseleziona tutti sulle righe dell’intervallo caricato. Estremi Da/A inclusivi nell’elenco filtrato, massimo 100 righe per invio, limite mostrato esplicitamente. Nome, EAN, SKU, quantità, peso, costo totale, vendita, minimo e commissione stimata modificabili; guadagno derivato al salvataggio. Supporto virgola decimale senza separatore delle migliaia.

Modifiche salvate nella sola anteprima: vista e listino originali restano invariati. I payload Kaufland e Worten usano i prezzi manuali salvati senza ricalcolo del ricarico. SKU composto aggiornato dopo modifiche a EAN/costo/minimo; un override SKU esplicito resta manuale anche nei salvataggi successivi. Valori invalidi e SKU duplicati impediscono la selezione delle righe interessate.

Salvataggio e conferma richiedono la versione SHA-256 dello snapshot letto. Il controllo sotto lock dell’invio impedisce sovrascritture e invio di valori cambiati da un’altra sessione. Le modifiche sono ammesse solo su bozze; dopo conferma, le righe restano immutabili. Permesso catalogo e isolamento Seller controllati prima di leggere il body. Nessuna nuova migrazione database.

## Validazione prima del rilascio

17 test backend pubblicazione superati, inclusi prezzi manuali fino al payload esterno simulato, selezione parziale, originali invariati, concorrenza, duplicati, isolamento Seller, numeri invalidi, CSV Worten e intervallo 2–3. 7 test frontend/proxy superati. Typecheck e build Next.js superati. Invii reali esclusi dal collaudo.

## Stato

Commit `135a56f27c21e529626676d5c80765a68095e03c` verificato Live su Render web, API e worker. Deploy rispettivi: `dep-dainu6gjo6nc73bplfl0`, `dep-dainu6gjo6nc73bplfpg`, `dep-dainu6ojo6nc73bplg30`.

Collaudo autenticato RioneroShop: vista Innpro di 3363 prodotti; intervallo 2–3 genera esattamente due righe. Deseleziona tutti: 0/2, seleziona tutti: 2/2, deselezione singola: 1/2. EAN 6930460000798: modifica vendita da 53,33 a 54,50 EUR accettata con virgola, salvata dal server, commissione ricalcolata a 8,18 EUR e guadagno a 6,82 EUR. Riapertura conferma 54,50 e 8,18. Prezzo e commissione originali ripristinati dopo il test. Nessuna offerta inviata al marketplace; conferma PUBBLICA vuota.

Promosso solo LEGACY-UI-0275 (editor Kaufland). L’editor comune Worten è coperto dai test del payload ma non da un account Worten reale, pertanto il criterio di collaudo Worten resta pendente. Copertura complessiva 164/2011 (8,16%).
