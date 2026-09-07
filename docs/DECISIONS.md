# Decisioni

## D-001 — Originale in sola lettura

Il programma Streamlit locale, il repository `marketplacehub-1` e il sito Streamlit pubblico sono riferimenti. Non vengono modificati.

## D-002 — Ricostruzione isolata

La ricostruzione avviene su `rebuild/streamlit-parity-v2`. Il precedente `main` resta pubblicato e il suo stato è archiviato su `archive/pre-rebuild-2026-09-07`.

## D-003 — Parità comportamentale

Le funzioni vengono trasferite con gli stessi input, formule, selezioni, persistenza, effetti collaterali, errori e output. L'architettura può cambiare; il comportamento operativo non viene reinterpretato.

## D-004 — Un blocco alla volta

Si implementa, collauda, documenta e pubblica un solo blocco prima di iniziare il successivo.

## D-005 — Percentuale riproducibile

La percentuale è l'indice `criteri verificati / criteri totali` generato da test originali, interazioni Streamlit e obblighi del Master Spec. Non rappresenta ore o date residue.

## D-006 — Backend autorevole

Tenant scope, permessi, formule, entitlement e idempotenza vengono applicati dal backend. Il frontend presenta il risultato e può offrire anteprime immediate senza diventare la fonte della regola.

## D-007 — Un solo design system

Seller, Agency e Platform condividono componenti e token; cambiano navigazione, densità e contenuti autorizzati.
