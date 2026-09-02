# Step 11 — v311 Server-side Product Table

## Obiettivo
La pagina **Lavora sui listini** non carica più l'intero catalogo normalizzato in RAM a ogni apertura/rerun.

## Cosa cambia
- filtri EAN/SKU/nome, quantità, costo e peso eseguiti direttamente su `catalog_products`;
- costo Cecotec risolto lato server per Paese tramite colonne indicizzabili/materializzate;
- paginazione reale da 100/250/500 prodotti;
- la UI riceve solo la pagina richiesta;
- selezione `tutti i filtrati` rappresentata come regola + eccezioni, non come DataFrame completo;
- selezione manuale mantenuta tra le pagine tramite `row_no`;
- modifiche economiche della pagina mantenute in sessione e applicate allo snapshot finale;
- l'intero set filtrato viene materializzato **solo al click su Salva vista**, quando è realmente necessario creare il file snapshot;
- i cataloghi v310 vengono reindicizzati una sola volta al nuovo schema v311 (`schema_version=2`).

## Prestazioni
Il costo ordinario di un rerun dipende dalla pagina richiesta (100–500 righe), non dal numero totale di prodotti del listino. Un catalogo da 500.000 prodotti non deve quindi generare un DataFrame da 500.000 righe per cambiare pagina o filtro.

## Compatibilità
Le viste già salvate restano nel formato PKL esistente. Il loro editor legacy non viene modificato in questo step; la migrazione delle viste salvate a storage/database server-side sarà un passaggio successivo.
