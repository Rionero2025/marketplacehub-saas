# Marketplace Hub — migrazione PostgreSQL locale

La versione v205 mantiene **SQLite come impostazione predefinita** e aggiunge PostgreSQL come backend supportato. Il passaggio è reversibile: il file `data/marketplace_hub.db` non viene eliminato.

## 1. Installa PostgreSQL su Windows

Installa PostgreSQL sullo stesso PC su cui gira Marketplace Hub. Durante l'installazione conserva la password dell'utente amministratore `postgres`.

## 2. Chiudi Marketplace Hub

Chiudi tutte le finestre Streamlit/Python del programma prima della copia dei dati.

## 3. Avvia la migrazione guidata

Esegui:

`MIGRA_A_POSTGRESQL_WINDOWS.bat`

Lo script:

1. installa/aggiorna le dipendenze Python, incluso Psycopg;
2. configura un utente PostgreSQL dedicato `marketplace_hub`;
3. crea/riusa il database locale `marketplace_hub`;
4. salva le credenziali in `data/database.toml` lasciando inizialmente `engine = "sqlite"`;
5. crea un backup consistente di SQLite in `data/backups/`;
6. copia tutte le tabelle e tutti i record in PostgreSQL;
7. riallinea le colonne identity;
8. confronta il numero di righe tabella per tabella;
9. attiva PostgreSQL **solo se la verifica finale è riuscita**.

Se la migrazione fallisce, SQLite rimane attivo.

## 4. Controlla dal programma

Dopo il riavvio apri la nuova voce **Database** nella barra laterale. Deve apparire `Database attivo: PostgreSQL` e il test di scrittura deve riuscire.

## Ritorno temporaneo a SQLite

Chiudi Marketplace Hub ed esegui:

`TORNA_A_SQLITE_WINDOWS.bat`

Il comando cambia soltanto il backend attivo. Non cancella PostgreSQL e non cancella SQLite.

## Ripetizione pulita della migrazione

`RIPETI_MIGRAZIONE_POSTGRESQL_PULITA.bat` svuota lo schema `public` del database PostgreSQL configurato e lo ricostruisce da SQLite. Usarlo solo sul database PostgreSQL dedicato a Marketplace Hub.

## Configurazione

La configurazione persistente è in `data/database.toml`. Essendo dentro `data`, deve essere conservata quando si sostituisce la cartella del programma.

Il pool predefinito usa 2–12 connessioni. I valori possono essere modificati nel file di configurazione:

- `postgresql_pool_min`
- `postgresql_pool_max`
- `postgresql_connect_timeout`

## Compatibilità v205

Per ridurre il rischio della migrazione, v205 mantiene inizialmente la struttura logica delle colonne esistenti (testi, interi e numeri reali) e converte il dialetto SQL SQLite usato dal programma in PostgreSQL. La normalizzazione futura di importi monetari a `NUMERIC` e JSON a `JSONB` può essere effettuata in una release successiva dopo il collaudo del nuovo backend con i dati reali.
