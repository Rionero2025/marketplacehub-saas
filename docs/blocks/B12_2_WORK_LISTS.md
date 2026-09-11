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
TypeScript e build Next da 20 pagine superati.


## Collaudo staging — 11 settembre 2026

Versione applicativa `6dd8328582b3af52a6b68f7c9ebfa328513e0915`, web, API e worker
Live su Render. Health API e readiness web HTTP 200; PostgreSQL e Redis up. Pagina autenticata `/seller/catalog/work`, negozio RioneroShop.

- Scelta InnPro LIGHT con FULL dello stesso fornitore: 5.449 prodotti, 109 pagine;
  selezione globale 5.449 → 0 → tutti, ricerca EAN 6930460000040 → una riga.
- Nome Charger SkyRC iMax B6AC V2, peso 1,03 kg, costo LIGHT 37,93 euro,
  prezzo iniziale 51,21 e minimo 41,72. Con spedizione aggiuntiva 2 euro:
  totale 39,93, prezzo 53,91 e minimo 43,92.
- Vista temporanea con destinazione Kaufland: prezzo modificato a 52,34,
  salvato e conservato nella riapertura. Nome prodotto e nome vista modificati;
  aggiunta riga TEST-CODEX con prezzo manuale 12,345 mantenuto dopo reload.
- Riga manuale rimossa e salvataggio riuscito; nuova lavorazione usata per
  sovrascrivere la stessa vista, un solo elemento presente nella raccolta.
- Eliminazione della sola vista di collaudo con conferma ELIMINA; raccolta finale
  vuota. Nessun prodotto pubblicato sui marketplace né feed modificato.

I criteri di interazione LEGACY-UI-0240–0251 sono verificati. Le regole specifiche
fornitori e gli altri requisiti di catalogo restano pendenti; questi 12 criteri
non certificano la parità completa del modulo.
