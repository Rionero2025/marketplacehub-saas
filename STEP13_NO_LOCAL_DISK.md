# Marketplace Hub v313 — No Local Disk / Durable Binary Storage

## Obiettivo
La v313 completa il passaggio iniziato in v312: il filesystem del container diventa **cache ricostruibile**, non archivio autorevole. Questo è necessario per eseguire più web process e worker senza richiedere che tutti vedano lo stesso disco locale.

## File resi durevoli
La v313 porta sul layer `ObjectStorage` i principali binari applicativi:

- listini originali caricati o scaricati, inclusi Excel/XML/CSV e cataloghi combinati PKL;
- snapshot delle viste salvate (già introdotti in v312);
- file sorgente di Tracciabilità (XLSX/XLS/CSV/TSV/TXT/PDF);
- documenti fornitore analizzati in Contabilità;
- export Excel della Contabilità;
- artifact generati dalla Creazione Prodotti / pubblicazione marketplace.

PostgreSQL conserva ID, metadati, SHA-256 e `storage_key`. Il contenuto pesante vive nello storage.

## Compatibilità
Il programma continua a creare una copia locale quando utile ai parser legacy, ma quella copia è una cache. Se scompare dopo un restart del container, viene ricreata dallo storage.

I file legacy vengono migrati progressivamente: quando un listino/export/tracking/artifact locale viene usato e non ha ancora una `storage_key`, Marketplace Hub lo copia nello storage e aggiorna il database. Non è necessario bloccare il programma per una migrazione monolitica.

## Backend
In sviluppo il backend predefinito resta `local` per non richiedere servizi esterni. In produzione SaaS va configurato un backend S3-compatible tramite le variabili già introdotte in v312 (`MARKETPLACE_HUB_STORAGE_BACKEND=s3`, bucket, endpoint, region e credenziali).

**Importante:** con backend `local` non si ottiene alta disponibilità multi-container. La modalità realmente “no local disk” richiede S3/R2/MinIO/GCS-S3 o equivalente.

## Sicurezza e integrità
Ogni oggetto durevole registra SHA-256 e dimensione. La lettura può verificare l'hash prima di consegnare il file all'applicazione. Le credenziali dello storage restano variabili d'ambiente e non vengono scritte nei job.

## Risultato architetturale
Dalla v313 un web server può sparire e venire ricreato senza perdere i file operativi durevoli. Questo prepara il passaggio a FastAPI + worker multipli e consente di scalare web e worker indipendentemente.
