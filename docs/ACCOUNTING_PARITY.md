# Contabilità: riferimento funzionale e separazione Seller

Requisito precisato il 4 settembre 2026. Riferimento online letto in sola lettura:
https://marketplace-hub-wchg.onrender.com/Contabilita
Il sito originale è distinto dal SaaS staging. Lo screenshot allegato mostra
la pagina Ordini del SaaS; il riferimento funzionale contabile è l’URL indicato.

## Regola di architettura

L’area Seller deve esporre la contabilità del proprio negozio. Il pannello Admin
e quello Agenzia possono scegliere il Seller su cui operare, secondo i permessi.
Ogni calcolo della pagina Contabilità resta riferito esclusivamente al singolo
Seller selezionato, ai suoi account e ai filtri applicati. Cambiare Seller deve
cambiare dati, listini, percentuali, selezioni ed export, senza risposte residue.
Nessuna somma tra Seller nella contabilità individuale. Eventuali viste aggregate
Agency/Admin sono funzioni separate da questa pagina e non sostituiscono i conteggi.

## Inventario verificato nel riferimento

| Funzione originale | Stato nel SaaS |
|---|---|
| Seller, marketplace e account espliciti | Scope backend per Seller/account presente; scelta marketplace distinta da allineare |
| Stato ultimo tentativo, successo, ultimo ordine, righe e ordini in memoria | Stato tecnico disponibile; riepilogo leggibile completo da portare |
| Aggiornamento ordini mancanti/modificati | Job e pulsante presenti |
| Risincronizzazione completa | Opzione backend presente; comando UI da portare |
| Selezione listini, tutti/nessuno, priorità EAN esatto | Selezione persistente e motore presenti; scorciatoie tutte/nessuno da portare |
| Ricalcolo con listini selezionati | Job e pulsante presenti |
| Filtri multipli fornitore, stato e nazione | Implementati in questa consegna, in verifica |
| Ricerca, periodo, righe visibili | Presenti |
| Vendite nette, commissioni, payout, margine utile, acquisti, rimborsi, margine lordo, costi mancanti | Riepilogo completato in questa consegna, in verifica |
| Quote gestore/Seller, percentuali del Seller corrente | Riepilogo completato in questa consegna, in verifica |
| Griglia editabile persistente senza salto al principio | Editor persistente presente; interazione diretta in cella ancora da allineare |
| Selezione righe, tutte filtrate, deselezione/azzeramento, selezione consecutiva | Da portare |
| Modifica massiva dei campi manuali sulle righe selezionate | Da portare |
| Documenti fornitore caricati/URL, OCR e abbinamento con conflitti e conferma | Da portare |
| Confronto Excel/URL, scelta foglio, soli campi mancanti, conflitti e conferma | Da portare |
| Excel delle righe selezionate e controllo precedenti esportazioni | Excel filtrato presente; selezione e controllo duplicati da portare |
| Archivio Excel persistente con download | Da portare e verificare su storage condiviso |
| PDF individuale/multipli marketplace con periodi giorno/settimana/mese/anno/intervallo e dettaglio opzionale | Da portare |

## Regole preservate

Calcoli e priorità provengono dal progetto Python originale. Costi da listini
abilitati e corrispondenza EAN/GTIN esatta; feed Innpro ingrosso riconosciuto tramite
type=light, type=full escluso dal costo automatico. Annullamenti/rimborsi seguono
il motore originale; override manuali persistenti. I valori mancanti sono segnalati,
non inventati. Le quote del riepilogo sono calcolate dal margine filtrato con le
percentuali del Seller, preservando l’arrotondamento residuo.

Il codice del repository originale marketplacehub-1 resta in sola lettura.
Questo inventario è un requisito di parità completa, non una dichiarazione che
le funzionalità ancora mancanti siano già disponibili. Totale verificato15%.


### Verifica del percorso dati contabile — 4 settembre 2026

Nuova verifica: nome prodotto, EAN, SKU composito, acquisto, vendita effettiva, commissioni, margini, quote e fonti attraversano il percorso fino alla API Seller; esposti nella tabella o nei dettagli riga. Testano realmente persistenza e risincronizzazione su dati sintetici. Import documenti/Excel, operazioni massive ed export archiviati rimangono nell’inventario da completare.
