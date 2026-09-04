# Storage condiviso e ripristino

Stato 4 settembre 2026: il Blueprint usa `local`; sul worker Render non risultano
variabili bucket/endpoint/access key/secret key né gruppi ambiente collegati.
Il valore del backend live è mascherato. Non è stata eseguita una prova S3/R2 live.
Il blocco 07 rimane aperto e non incrementa la percentuale.

## Configurazione richiesta

Usare un bucket privato S3 o R2 e lo stesso gruppo ambiente per API e worker:

| Variabile | Valore |
|---|---|
| MARKETPLACE_HUB_STORAGE_BACKEND | s3 |
| MARKETPLACE_HUB_STORAGE_BUCKET | nome del bucket scelto |
| MARKETPLACE_HUB_STORAGE_PREFIX | marketplacehub/staging |
| MARKETPLACE_HUB_STORAGE_ENDPOINT_URL | endpoint R2/S3 compatibile; vuoto per AWS |
| MARKETPLACE_HUB_STORAGE_REGION | regione AWS oppure auto per R2 |
| MARKETPLACE_HUB_STORAGE_ACCESS_KEY | credenziale nel gruppo ambiente Render |
| MARKETPLACE_HUB_STORAGE_SECRET_KEY | segreto nel gruppo ambiente Render |

Non inserire segreti nel repository. Non cambiare il backend finché il bucket non
è disponibile. Il `value: local` nel Blueprint va riallineato alla configurazione
scelta prima di applicare una nuova sincronizzazione del Blueprint.

Prima del cambio backend recuperare i file locali esistenti e un backup del
database. Le righe già archiviate con backend local devono essere copiate nello
storage remoto mantenendo chiavi e hash, non soltanto contrassegnate migrate.
Le funzioni legacy `migrate_*_to_storage` saltano le righe con chiave già presente:
non sono sufficienti per una migrazione local → S3. Verificare inventario e hash
prima di abbandonare copie locali. Questa migrazione live non è ancora eseguita.

## Prova API → worker senza cache

1. Dalla shell del servizio API eseguire `python -m tools.storage_probe write`.
   Il comando crea 256 byte casuali in `_probes/` e restituisce chiave e SHA-256,
   senza usare dati cliente o accedere al database.
2. Dalla shell del worker eseguire
   `python -m tools.storage_probe restore --key CHIAVE --sha256 HASH`.
3. Riavviare il worker dopo i lavori in corso e ripetere il restore aggiungendo
   `--cleanup`. Atteso: `verified: true`, `size_bytes: 256`, `cleanup: true`.
4. Ripetere nell'altra direzione per verificare i permessi di entrambi i servizi.
5. Registrare servizio, commit, orario e risultato senza credenziali.

Questa prova verifica persistenza e condivisione di un oggetto, non il disaster
recovery completo del database. Backup DB e manifest oggetti devono riferirsi a
versioni coerenti e saranno verificati nel blocco backup/amministrazione.

## Integrità e versioni

Le cache vengono verificate tramite SHA-256 e recuperate dallo storage quando
corrotte. I file temporanei sono privati per ogni scrittore; i percorsi cache
includono il digest. Gli snapshot delle viste e i listini archiviano versioni
immutabili. Le versioni precedenti non vengono cancellate automaticamente mentre
lettori o backup potrebbero riferirle: definire retention e garbage collection
dopo l'inventario dei riferimenti e dei backup. Le viste pickle rimangono un
formato interno legacy; non devono essere caricate da sorgenti non fidate.

La CI copre ripristino senza cache, cache/oggetti corrotti, concorrenza e fallimento
dell'aggiornamento metadata. I test locali non dimostrano una configurazione live.
