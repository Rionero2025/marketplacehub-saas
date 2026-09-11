# B12.1b2 — Feed InnPro FULL/LIGHT

Data: 11 settembre 2026. Perimetro: pannello Seller Enterprise, senza limiti
commerciali di pacchetto. Il programma Streamlit originale resta in sola lettura.

Stato del documento: **GATE LOCALE COMPLETATO; PUBBLICAZIONE E COLLAUDO
FUNZIONALE STAGING IN ATTESA**.

## Risultato implementato

Catalogo → Listini espone ora, per sorgenti File e URL:

- formato `Generico` con ruolo `Standard`;
- formato `InnPro IOF` con scelta obbligatoria `FULL` o `LIGHT`;
- descrizioni visibili che chiariscono il compito dei due feed;
- badge del formato e del ruolo nei listini già importati;
- contratto API coerente per upload multipart e feed JSON.

Il backend conserva `provider` e `feed_role` come identità del listino e consente
FULL e LIGHT sullo stesso fornitore. Gli upload esistenti vengono interpretati come
`generic + standard`, senza attribuire retroattivamente un ruolo InnPro.

## Worker e parser InnPro

Il worker usa un percorso dedicato per InnPro:

1. scarica il feed remoto su disco entro 200 MiB e una deadline complessiva;
2. analizza l'XML IOF in streaming, con namespace indipendenti;
3. verifica che il ruolo rilevato coincida con quello configurato;
4. normalizza i prodotti e prepara l'artefatto durevole;
5. attiva versione, righe e job completato nella transazione prevista dal modulo
   Catalogo;
6. elimina il file temporaneo anche in caso di errore.

Il percorso generico continua a usare il limite da 20 MiB. Il feed InnPro grande
viene compresso con gzip deterministico prima della persistenza; hash e dimensione
restano quelli dell'originale. La lettura dell'artefatto verifica i dati decompressi
e fallisce esplicitamente in caso di corruzione.

Il parser non conserva il documento XML in memoria: libera anche i sottoalberi
estranei ai prodotti e sposta le righe normalizzate su un file temporaneo oltre
1.000 righe o 8 MiB. Sul FULL originale, parsing, gzip e persistenza SQLite di
6.880 prodotti hanno raggiunto **129,31 MiB RSS**, lasciando circa 382 MiB di
margine rispetto al worker Render da 512 MiB. Una prova con 200.000 nodi XML
estranei verifica inoltre un picco `tracemalloc` inferiore a 16 MiB.

## Integrazione con gli ordini

La risoluzione del costo nel flusso Ordini interroga soltanto una versione InnPro
LIGHT attiva appartenente allo stesso Seller, organizzazione e ambiente. La
corrispondenza è per EAN esatto. FULL, SKU e SKU composto non sono fallback ammessi.

Un errore del repository o una corrispondenza non dimostrata non conserva né crea
un costo ricavato da una sorgente diversa. Questo comportamento fail closed evita
che una scheda FULL o i dati di un altro tenant alterino costo, utile e margine.

La sincronizzazione Ordini e ogni attivazione, aggiunta o rimozione di un LIGHT
usano lo stesso lock stabile del Seller e la stessa transazione del dato salvato.
Il ricalcolo dello storico procede per chiave in pagine da 250 righe: se un listino
viene rimosso applica il LIGHT precedente oppure pulisce i riferimenti non più
validi, conservando soltanto un override manuale esplicito. Gli alias InnPro sono
riconosciuti come token delimitati, evitando che nomi diversi come `Finn Products`
vengano associati per una sottostringa accidentale.

## Migrazioni

- `20260911_0012_catalog_feed_identity.py` aggiunge provider e ruolo, vincola le
  combinazioni valide e migra i record esistenti a `generic + standard`;
- `20260911_0013_catalog_artifact_encoding.py` aggiunge codifica, dimensione
  memorizzata e supporto al formato IOF per gli artefatti durevoli;
- il ponte schema e la coda Catalogo dedicata consentono il deploy progressivo
  senza consegnare un nuovo job InnPro a un worker precedente;
- il downgrade rifiuta esplicitamente dati gzip, IOF o artefatti non rappresentabili
  nello schema precedente, anziché perderli.

## Verifiche locali acquisite

- suite Python completa: **615/615 test superati**;
- parser InnPro, inclusi limiti e memoria dei nodi estranei: **16/16**;
- costi InnPro, fallback, cancellazioni, transazioni e concorrenza: **25/25**;
- cutover della coda e recupero job: **13/13**;
- app-web: **156/156 test superati**;
- TypeScript, Ruff e `pip check`: **superati**;
- build Next.js: **19/19 pagine generate**;
- Alembic: singola head `20260911_0013`;
- prova end-to-end transitoria sui due file originali: **superata**.

I dati esatti della prova reale sono:

| Feed | Byte originali | SHA-256 | Prodotti/righe |
| --- | ---: | --- | ---: |
| FULL | 111.012.011 | `b7fcb8a17f89f7e733e03a0b21569b87660dcd1ace1407ecf001cbaa4976f17e` | 6.880 |
| LIGHT | 1.621.952 | `9cf2a50e8be89eff8f501b3e08157f8788bdaba951aecc99067283309c0f4b57` | 5.000 |

Queste evidenze chiudono il gate locale. Pubblicazione e collaudo autenticato nello
staging restano separati e non vengono dichiarati completati in anticipo.

## Criteri candidati

| ID | Funzione candidata | Stato ledger |
| --- | --- | --- |
| `MASTER-0228` | feed InnPro FULL/LIGHT distinti | `pending` |
| `MASTER-0229` | selezione della sorgente appropriata | `pending` |
| `MASTER-0230` | costo all'ingrosso corretto dal LIGHT | `pending` |

Il ledger resta a **147/2.011 = 7,31%**. Solo dopo deploy e collaudo autenticato
dei tre criteri il totale potrà diventare **150/2.011 = 7,46%**. I sette criteri
di B12.1b1 restano separatamente pending; se tutti i dieci criteri B12.1b1+B12.1b2
fossero verificati nello staging, il totale sarebbe **157/2.011 = 7,81%**.

## Cutover sicuro su Render

I job Catalogo nuovi usano la coda dedicata `marketplace-hub-catalog-v2`. Gli
Ordini restano sulla coda legacy `marketplace-hub`. Il worker candidato ascolta
entrambe con `RoundRobinWorker`: dopo un job servito, RQ ruota la coda di partenza
e impedisce a una sequenza di importazioni Catalogo di affamare gli Ordini.

Questa separazione elimina il rischio principale del deploy sovrapposto: un
worker vecchio, che conosce soltanto `marketplace-hub`, non può prelevare un job
InnPro pubblicato dalla nuova API. Se la nuova API diventa raggiungibile prima del
nuovo worker, il job resta durevolmente in attesa sulla coda v2 e conserva lo
stesso identificatore `catalog-feed:<uuid>` usato dal recupero applicativo.

### Gate prima del rilascio

1. Congelare temporaneamente le azioni Importa/Aggiorna Catalogo e attendere che
   non risultino job Catalogo attivi.
2. Annotare lo SHA completo candidato con `git rev-parse HEAD` e verificare che il
   tree contenga soltanto le modifiche approvate con `git status --short`.
3. Disabilitare l'auto-deploy dei servizi coinvolti oppure usare, per ognuno, il
   deploy manuale dello stesso commit. Un semplice nome di branch non è una prova
   che i container eseguano lo stesso codice.
4. Associare nel Dashboard Render i servizi reali a API, worker e app-web. I nomi
   presenti in `render.yaml` (`marketplacehub-v2-*`) e gli URL staging attualmente
   osservati (`marketplacehub-saas-*`) non coincidono; la service ID mostrata dal
   Dashboard è l'autorità per il cutover.

### Ordine preferito: migrazioni → worker → API/web

1. Eseguire `alembic upgrade head` con l'immagine candidata e le variabili dello
   staging. Il log deve mostrare il completamento delle revisioni
   `20260911_0012` e `20260911_0013`; `alembic current` deve poi restituire
   `20260911_0013 (head)`.
2. Pubblicare il worker allo stesso SHA. Nei log di avvio deve comparire l'ascolto
   di entrambe le code, nell'ordine
   `marketplace-hub-catalog-v2,marketplace-hub`. Attendere che il deploy sia
   `Live` e che la vecchia istanza sia terminata prima di riaprire gli import.
3. Pubblicare l'API allo stesso SHA e attendere `/health/ready` verde. Il
   `preDeployCommand: alembic upgrade head` già dichiarato in `render.yaml` è
   idempotente e costituisce una seconda verifica dello schema.
4. Pubblicare app-web allo stesso SHA; riaprire le azioni Catalogo soltanto dopo
   che health check e SHA risultano coerenti sui tre servizi.

Il repository attuale include migrazioni e `alembic.ini` nell'immagine API, ma
non nell'immagine worker. Per rispettare letteralmente il primo passo occorre
quindi un job di migrazione separato costruito dallo SHA candidato, oppure una
release successiva che includa le migrazioni nell'immagine worker e configuri su
quel servizio lo stesso pre-deploy. Se Render consente soltanto il pre-deploy
dell'API, usare questa variante sicura: mantenere il Catalogo congelato, distribuire
prima l'API per eseguire le migrazioni, quindi worker e app-web. La coda dedicata
fa sì che eventuali job inviati per errore dalla nuova API restino in attesa e non
possano essere consumati dal worker vecchio.

### Verifica del commit live e dei servizi

Per ciascun servizio aprire **Render Dashboard → Deploys/Events → deploy Live** e
registrare service ID, SHA completo, orario e stato. Gli endpoint pubblici
dimostrano disponibilità, ma non espongono oggi lo SHA e non sostituiscono questa
verifica.

Comandi di controllo da PowerShell:

```powershell
$api = "https://marketplacehub-saas-api-staging.onrender.com"
$web = "https://marketplacehub-saas-web-staging.onrender.com"
Invoke-RestMethod "$api/health/live"
Invoke-RestMethod "$api/health/ready"
Invoke-WebRequest "$api/openapi.json" -UseBasicParsing
Invoke-RestMethod "$web/health"
Invoke-RestMethod "$web/api/auth/readiness"
```

Sul worker, che non ha un endpoint pubblico, usare stato Live e log Render. Da
una shell con `MH_REDIS_URL` disponibile, la fotografia delle code è:

```powershell
rq info -u $env:MH_REDIS_URL --only-queues marketplace-hub-catalog-v2 marketplace-hub
```

`/health/live` ha restituito in una verifica pre-release
`environment: development` sull'API staging. Correggere o spiegare questa deriva
di configurazione prima di chiudere il gate; un HTTP 200 da solo non basta.

### Smoke test e rollback con drain

Dopo il cutover, creare prima un'importazione piccola e verificare in ordine:

1. stato iniziale `queued` sulla coda v2 e assenza del job sulla coda legacy;
2. transizione `queued → started → done` con il worker candidato;
3. un job Ordini completato dalla coda legacy durante l'attività Catalogo;
4. un FULL e un LIGHT attivi sullo stesso fornitore, quindi costo risolto dal solo
   LIGHT per EAN esatto;
5. recupero coerente dopo riavvio del worker e lettura integra dell'artefatto gzip.

Per il rollback, bloccare prima nuovi import/refresh. Lasciare il worker candidato
in esecuzione finché `marketplace-hub-catalog-v2` è vuota e tutti i job avviati
sono terminali; in alternativa, risolvere esplicitamente ogni job prima di
procedere. Ripristinare app-web e API, verificare health e comportamento legacy,
e retrocedere il worker per ultimo. Un worker vecchio non ascolta la coda v2: se
lo si ripristina prima del drain, i job rimangono bloccati. Non eseguire il
downgrade dello schema dopo artefatti InnPro/IOF o gzip senza aver superato i
controlli di sicurezza delle migrazioni, che devono rifiutare dati non
rappresentabili anziché perderli.

## Collaudo staging ancora richiesto

Il collaudo deve confermare almeno:

1. presenza e invio corretto delle scelte Generico/InnPro e FULL/LIGHT per File e
   URL;
2. coesistenza di FULL e LIGHT sul medesimo fornitore;
3. importazione asincrona, avanzamento e attivazione della sola versione valida;
4. contenuti provenienti dal FULL e costo/stock provenienti dal LIGHT;
5. calcolo costo per EAN esatto nell'archivio Ordini;
6. isolamento fra Seller e comportamento fail closed;
7. lettura integra dell'artefatto FULL compresso dopo il riavvio dei servizi.
