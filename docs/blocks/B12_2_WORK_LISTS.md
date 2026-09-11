# B12.2 — Lavora sui listini

Fonte letta senza modifiche: `pages/3_Lavora_sui_Listini.py`, righe 1–347,
e `services/lists.py`, normalizzazione e filtro peso. Perimetro Seller Enterprise.

## Flusso trasferito

- Sezione Catalogo → Lavora sui listini, distinta dall'importazione.
- Scelta del listino e ricerca letterale per EAN/SKU/nome, quantità minima,
  costo minimo e massimo (zero significa nessun massimo).
- Esclusione per peso e dimensioni note: soglie strette sopra/sotto, intervallo
  inclusivo, misure sconosciute mantenute. Nessuna unità InnPro presunta.
- Spedizione del feed + aggiunta; costo totale; prezzo e prezzo minimo con
  ricarichi separati, inizialmente 35% e 10%.
- Stessa sequenza numerica pandas: float, moltiplicazione per 100, arrotondamento
  all'intero pari, divisione per 100. Quaranta risultati di riferimento sono stati
  generati con pandas eseguendo le espressioni originali, inclusi casi di centesimo
  che differiscono da Decimal.quantize (13,50 × 1,35 → 18,23).
- Selezione su tutte le pagine, modifiche economiche esplicite e indipendenti,
  destinazioni fra gli account attivi autorizzati, nome, creazione o sostituzione.
- Riapertura delle viste, modifica di identificativi/nome/peso/valori, inserimento
  manuale, rimozione delle righe, modifica delle destinazioni e cancellazione con
  conferma ELIMINA. Le destinazioni salvate non causano pubblicazioni marketplace.

## Persistenza SaaS

La migrazione 0015 aggiunge viste e righe snapshot. I prodotti del fornitore non
sono modificati; il risultato salvato non viene ricalcolato automaticamente al
cambiare del feed. Ricetta e versioni di origine restano nella vista. Il browser
riceve 50 prodotti per pagina; il salvataggio legge e scrive batch da 100 righe,
senza caricare l'XML o tutto il catalogo in memoria.

Ogni operazione verifica Seller, organizzazione e permessi. Prodotti selezionati,
modifiche e destinazioni devono appartenere al perimetro autorizzato. Il salvataggio
blocca le sorgenti e controlla le versioni; gli aggiornamenti di viste controllano
una revisione per impedire sovrascritture di modifiche concorrenti. Una risposta
incerta blocca nuovi salvataggi fino a verifica delle viste.

## InnPro e limiti dichiarati

Il LIGHT fornisce costo e stock. Un FULL scelto dello stesso fornitore completa
nome e misure per EAN esatto univoco: nessun fallback del costo su FULL. Il FULL
da solo non è una sorgente prezzi da lavorare. Le regole specifiche Cecotec
multi-Paese e ActiveShop Diamond/pack_type non sono ancora trasferite e tali
fornitori non possono usare silenziosamente il calcolo generico.

Questo blocco non completa pubblicazione, template, contenuti multilingua o tutte
le lavorazioni specialistiche presenti nel progetto originale.

## Verifiche

39 test Python mirati (API, autorizzazioni, isolamento, lifecycle snapshot,
selezione oltre la prima pagina, arrotondamenti e migrazione); 163 test web;
TypeScript e build Next da 20 pagine superati. Collaudo staging da registrare
prima di promuovere i criteri LEGACY-UI-0240…0251 nel ledger.
