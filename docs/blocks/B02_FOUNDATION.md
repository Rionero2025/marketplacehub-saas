# B02 — Fondazione tecnica

Stato: completato il 7 settembre 2026.

## Risultato

Il ramo contiene una struttura eseguibile separata per sito pubblico, app SaaS, API e worker. PostgreSQL è il database durevole; Redis è usato per coda e stato temporaneo. Nessuna logica Seller o marketplace è stata anticipata in questo blocco.

## Componenti consegnati

- workspace pnpm con Next.js 16, React 19 e TypeScript;
- due build autonome: `marketing-web` e `app-web`;
- pacchetti frontend condivisi per UI, tipi e configurazione;
- API FastAPI con liveness, readiness e request ID;
- readiness predisposta per PostgreSQL e Redis senza esporre il testo degli errori;
- core Python condiviso con configurazione tipizzata e URL segreti;
- pool PostgreSQL configurabile con pre-ping;
- worker RQ in processo e container separati;
- migrazione Alembic iniziale con upgrade e downgrade verificati;
- Compose locale con sei servizi;
- Blueprint Render staging con quattro processi, PostgreSQL e Key Value;
- lock frontend e Python e immagini container versionate.

## Verifiche eseguite

- 8 test Python di fondazione;
- lint Ruff;
- quattro typecheck TypeScript;
- build di produzione di entrambe le applicazioni Next.js;
- generazione SQL Alembic upgrade e downgrade;
- controllo strutturale Compose/Render e scansione dei formati di segreto noti;
- validazione del Blueprint contro lo schema JSON ufficiale Render;
- verifica dei percorsi standalone generati da Next.js.

Docker non è disponibile sull'host di sviluppo, quindi le immagini non sono state eseguite localmente. Il collaudo container e Render resta parte del blocco finale di staging, e i relativi criteri non vengono segnati come verificati.
