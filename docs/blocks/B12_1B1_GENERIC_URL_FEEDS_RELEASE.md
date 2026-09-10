# B12.1b1 — Feed listino generici da URL HTTPS

Data: 10 settembre 2026. Perimetro: pannello Seller Enterprise, senza limiti
commerciali di pacchetto. Il programma Streamlit originale resta in sola lettura.

Stato del documento: **IMPLEMENTAZIONE VERIFICATA; COLLAUDO FUNZIONALE STAGING IN
ATTESA**.

## Risultato trasferito

La pagina **Catalogo → Listini** conserva il caricamento da file e aggiunge il flusso
generico da URL presente nell'originale:

- scelta esplicita dell'origine `File` oppure `URL`;
- collegamento di un feed HTTPS con credenziali HTTP Basic facoltative;
- importazione asincrona di CSV, TXT, TSV, XLS, XLSX e XML;
- stato durevole del job e barra di avanzamento determinata o indeterminata;
- anteprima normalizzata della sola versione attiva;
- modifica dell'URL principale con azioni separate per mantenere, sostituire o
  rimuovere le credenziali;
- comando **Aggiorna ora dal feed** senza creare un nuovo listino;
- conservazione della versione precedente quando download, parsing o autorizzazione
  falliscono;
- eliminazione bloccata finché un aggiornamento del listino è in coda o in esecuzione.

Il browser mostra soltanto l'host pubblico. URL completo, query e credenziali non
vengono restituiti dai DTO e non sono precompilati nell'editor. Il salvataggio di una
nuova configurazione non avvia automaticamente il download.

## Persistenza, worker e migrazione

La migrazione `20260910_0011_catalog_url_versions.py` introduce versioni immutabili e
job di aggiornamento nello scope `organization_id + seller_id + price_list_id`.
L'attivazione di artefatto, prodotti normalizzati, versione e job concluso avviene in
un'unica transazione. Un hash già acquisito riusa la versione esistente.

Il worker riceve soltanto l'UUID del job, rilegge autorizzazioni e revisione della
configurazione e decifra il segreto soltanto nel processo di background. I job RQ
terminati in modo anomalo o mancanti oltre la soglia vengono chiusi con stato di errore
esplicito, senza retry automatico e senza lasciare l'interfaccia bloccata.

Il backfill degli upload esistenti usa `INSERT … SELECT` lato database, senza caricare
in memoria gli artefatti BYTEA. Un trigger ponte PostgreSQL/SQLite crea la versione 1
anche se un processo della release precedente completa un upload durante il deploy;
il nuovo repository evita inserimenti duplicati. Il downgrade rimuove prima il ponte e
conserva il solo snapshot attivo rappresentabile dallo schema precedente.

## Confini di sicurezza

- HTTPS obbligatorio, porta 443, TLS verificato, massimo tre redirect;
- risoluzione DNS ripetuta a ogni redirect e connessione fissata a un IP già validato;
- rifiuto di IP privati, loopback, link-local, multicast, riservati, site-local,
  unspecified e prefissi NAT64;
- fallback fra indirizzi pubblici multipli entro un unico deadline globale;
- credenziali eliminate definitivamente dopo un redirect cross-origin;
- credenziali esistenti conservabili soltanto se il nuovo host coincide con quello
  autorevole già salvato;
- limite download 20 MiB, timeout DNS/connessione/lettura/totale e nomi file sanificati;
- autenticazione e permesso `CATALOG` in scrittura prima della lettura di ogni JSON o
  multipart; JSON limitato a 16 KiB anche senza `Content-Length` attendibile;
- un solo job attivo per listino e lock PostgreSQL ordinati `job → listino` prima di
  modifica, attivazione o cancellazione.

## Verifiche locali acquisite

- suite Python completa: **533/533 test superati**;
- suite Catalogo: **132/132 test superati**;
- suite app-web: **153/153 test superati**, incluso TypeScript;
- build Next.js produzione: **superata, 19/19 pagine generate**;
- Ruff, `pip check`, `git diff --check` e unica head Alembic
  **`20260910_0011`**: superati;
- audit indipendente su SSRF, segreti, tenancy, transazioni/job, API/BFF/UI e
  migrazione PostgreSQL: **GO, nessun blocker P0/P1/P2 residuo**.

## Criteri candidati

Questi criteri hanno implementazione e prove locali, ma restano `pending` finché il
flusso autenticato non viene collaudato nello staging:

| ID | Funzione candidata | Stato ledger |
| --- | --- | --- |
| `LEGACY-UI-0042` | scelta origine File/URL | `pending` |
| `LEGACY-UI-0048` | URL del feed | `pending` |
| `LEGACY-UI-0054` | modifica URL catalogo principale | `pending` |
| `LEGACY-UI-0057` | salvataggio URL feed | `pending` |
| `LEGACY-UI-0060` | aggiornamento immediato dal feed | `pending` |
| `MASTER-0767` | download listino remoto | `pending` |
| `MASTER-0769` | barra di avanzamento | `pending` |

Il ledger resta quindi a **147/2.011 = 7,31%**. Se tutti e sette i criteri superano il
collaudo staging, il totale diventerà **154/2.011 = 7,66%**.

## Perimetro successivo

Questo sottoblocco implementa il feed HTTPS generico. Restano separati gli adapter e i
flussi multipli specifici di Hurtel, Cecotec, ActiveShop, ForceTop, AB Online e InnPro
FULL/LIGHT, la pianificazione periodica, la condivisione Agency/Platform e il ricalcolo
dei costi nell'archivio Ordini.
