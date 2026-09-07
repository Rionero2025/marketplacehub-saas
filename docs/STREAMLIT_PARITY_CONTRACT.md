# Contratto di parità con Marketplace Hub Streamlit

## Regola principale

Il comportamento del programma Streamlit originale è la specifica. Ogni funzione viene trasferita senza reinterpretarla. La nuova architettura separa interfaccia web, API, core, worker, database, cache e storage, ma conserva il flusso operativo osservabile.

Le modifiche esplicite dell'utente documentate in `docs/DECISIONS.md` prevalgono sulla
parità. D-013 esclude dal SaaS la ripartizione degli utili nostro/partner in tutti i portali;
restano richiesti margine e utile del singolo Seller. Non reintrodurre quote o percentuali
di ripartizione nei blocchi Ordini, Dashboard, Contabilità o Agency.

## Cosa significa “identico”

Per ogni pagina devono essere inventariati e riprodotti:

- prerequisiti, permessi e Seller attivo;
- campi, valori iniziali, convalide e messaggi;
- pulsanti e ordine delle operazioni;
- filtri, selezioni e persistenza dello stato;
- formule, arrotondamenti, casi nulli e regole di esclusione;
- chiamate marketplace e politiche di retry;
- scritture e letture dal database;
- file importati, generati e archiviati;
- operazioni lunghe, avanzamento, errori e ripresa;
- risultati visibili e download.

Una funzione non è completata perché esiste un servizio con nome simile. È completata quando il percorso intero produce lo stesso risultato con casi equivalenti.

## Vincoli

- Il repository e l’installazione Streamlit originali sono in sola lettura.
- Non introdurre nuovi flussi, limiti, conferme o formule per preferenza progettuale.
- Migliorie tecniche di sicurezza e concorrenza sono ammesse solo se trasparenti per il comportamento funzionale.
- Seller, Agency e Platform Admin hanno superfici diverse. Ogni calcolo di un Seller usa soltanto dati, account e listini di quel Seller; eventuali aliquote contabili non sono percentuali di ripartizione dell'utile.
- Il design viene rifinito dopo la parità funzionale, salvo l’usabilità necessaria a collaudare il flusso.

## Metodo obbligatorio per ogni blocco

1. Leggere pagina Streamlit e tutti i servizi chiamati.
2. Scrivere una matrice input → trasformazione → persistenza → output.
3. Elencare stato di sessione, effetti collaterali e casi di errore.
4. Preparare casi di regressione con risultati attesi dal codice originale.
5. Implementare core e persistenza multi-tenant.
6. Esporre API e worker senza spostare formule nel frontend.
7. Riprodurre il flusso nell’interfaccia web.
8. Confrontare risultato originale e SaaS.
9. Pubblicare solo dopo test automatici e verifica dello staging.

Gli stati ammessi sono: assente, in analisi, parziale, equivalente verificato, non conforme. Le percentuali derivano soltanto dall’inventario delle funzioni atomiche verificato.
