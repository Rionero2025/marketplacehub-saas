# Marketplace Hub v308 — Packlink + Tracking Speed

## Obiettivo
Portare Packlink PRO e Tracciabilità nello stesso Core/Worker Engine usato da ordini, Buy Box e Contabilità.

## Modifiche
- `PacklinkCore`: profilo magazzini/pacchi con shared cache (Redis-ready) e sincronizzazione spedizioni come job persistente.
- La pagina Packlink non blocca più l'interfaccia durante `Scarica / aggiorna spedizioni Packlink`.
- `TrackingCore`: lettura ordini limitata direttamente al periodo SQL, analisi documenti e matching UI-agnostici.
- Aggiornamento ordini della pagina Tracciabilità eseguito tramite worker.
- Upload documenti: il file viene archiviato una sola volta e nel job passa soltanto l'ID; gli URL vengono scaricati dal worker.
- Parsing + riconoscimento fornitore + matching + persistenza import vengono eseguiti in background.
- I risultati persistiti vengono ricaricati dal DB senza riparsare i file.

## Sicurezza
Le API key Packlink e le credenziali marketplace non entrano nel payload dei job. Il worker le recupera dal database cifrato.

## Prossimo passo
Object storage per listini, documenti, snapshot ed export, eliminando la dipendenza dai file locali del processo web.
