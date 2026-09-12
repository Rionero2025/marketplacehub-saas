# B16.3 — Selezione tutti o intervallo

Due checkbox alternative: seleziona tutti i prodotti (default) oppure seleziona dal numero iniziale al finale, inclusi. Numerazione sull’elenco dopo i filtri. Rimosso il limite di 100 prodotti: anteprima completa con visualizzazione di 100 righe per pagina e selezione/modifiche conservate tra pagine. Seleziona tutti e deseleziona tutti agiscono sull’intera anteprima. Le righe non valide restano escluse dall’invio.

Preparazione letta in batch e inserimenti a gruppi di 200. Lo storico carica solo conteggi, senza decodificare tutti i prodotti delle 30 bozze. Limite tecnico esplicito di 20.000 righe per anteprima: oltre tale dimensione errore senza troncamento, con invito a scegliere un intervallo. Body conferma fino a 1 MiB, modifiche fino a 8 MiB; isolamento e controllo versione invariati. Vecchie bozze senza selection_mode conservano il significato range.

Verifiche: 18 test backend pubblicazione, inclusi 425 prodotti tutti, intervallo 101–301 di 201 righe e conferma HTTP sopra 16 KiB senza chiamate marketplace; 9 test frontend/DTO/proxy, TypeScript e build 21 pagine. Collaudo staging da completare. Nessun nuovo criterio finale promosso: 165/2011, 8,20%.
