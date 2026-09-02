# Marketplace Hub v309 — Packlink Mass Engine

## Obiettivo
Portare fuori dal rendering Streamlit anche le due operazioni Packlink più costose e delicate:
calcolo tariffe su molti ordini e creazione massiva delle spedizioni pronte per pagamento.

## Cosa cambia
- `packlink.quotes.mass`: tariffe calcolate in background e in parallelo (default 6 worker, massimo 12).
- Le tariffe sono persistite in PostgreSQL (`packlink_mass_quote_cache`) e la UI legge il risultato senza rifare le chiamate.
- `packlink.drafts.mass`: creazione massiva in background con concorrenza prudente (default 2, massimo 4).
- Gli ordini sono ricostruiti dalla cache persistente del marketplace nel worker: nome cliente, indirizzo ed email non vengono duplicati nelle task della coda.
- Le API key Packlink/marketplace non entrano mai nel payload del job.

## Protezione doppie spedizioni
La tabella `packlink_draft_guards` introduce una chiave idempotente per ordine.
Un ordine non forzato già creato non viene POSTato una seconda volta. Se la connessione si interrompe dopo l'inizio del POST e non è possibile sapere con certezza se Packlink abbia creato la bozza, lo stato diventa `uncertain`: il worker NON ritenta automaticamente. L'operatore deve prima verificare Packlink e solo dopo, se necessario, usare la forzatura esplicita.

## Architettura
Streamlit -> background_jobs -> PacklinkCore -> Packlink API -> PostgreSQL.
Lo stesso contratto può essere eseguito dal worker Render dedicato senza modificare la pagina.
