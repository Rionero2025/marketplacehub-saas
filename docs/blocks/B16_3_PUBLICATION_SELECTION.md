# B16.3 — Selezione tutti o intervallo

Due checkbox alternative: seleziona tutti i prodotti (default) oppure seleziona dal numero iniziale al finale, inclusi. Numerazione sull’elenco dopo i filtri. Rimosso il limite di 100 prodotti: anteprima completa con visualizzazione di 100 righe per pagina e selezione/modifiche conservate tra pagine. Seleziona tutti e deseleziona tutti agiscono sull’intera anteprima. Le righe non valide restano escluse dall’invio.

Preparazione letta in batch e inserimenti a gruppi di 200. Lo storico carica solo conteggi, senza decodificare tutti i prodotti delle 30 bozze. Limite tecnico esplicito di 20.000 righe per anteprima: oltre tale dimensione errore senza troncamento, con invito a scegliere un intervallo. Body conferma fino a 1 MiB, modifiche fino a 8 MiB; isolamento e controllo versione invariati. Vecchie bozze senza selection_mode conservano il significato range.

Verifiche: 18 test backend pubblicazione, inclusi 425 prodotti tutti, intervallo 101–301 di 201 righe e conferma HTTP sopra 16 KiB senza chiamate marketplace; 9 test frontend/DTO/proxy, TypeScript e build 21 pagine. Collaudo staging completato il 12 settembre 2026. Nessun nuovo criterio finale promosso: 165/2011, 8,20%.

## Rilascio e collaudo autenticato

Commit a8a1cdcd5cad316d6493d7694473367cb1e5602d Live su tutti i servizi Render. Web dep-daioaomk1f9s73f8achg, API dep-daioaomk1f9s73f8acm0, worker dep-daioaomk1f9s73f8acvg.

RioneroShop, vista Innpro, account Kaufland principale: anteprima Tutti delle 19:04:22 con 3363/3363 selezionati, 34 pagine. Deseleziona tutti produce 0/3363 anche in pagina 2; Seleziona tutti dalla pagina 2 ripristina 3363/3363 e checkbox riga 101 selezionato. Intervallo 101–301 delle 19:05:04: esattamente 201 prodotti selezionati su 3 pagine, estremi inclusi. Ripresa infine l’anteprima completa. Nessuna offerta inviata e nessun prezzo modificato. Il limite di 100 è solo di visualizzazione per pagina. Nessun criterio finale aggiuntivo: totale invariato 8,20%.
