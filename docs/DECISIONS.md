# Decisioni

## D-001 — Originale in sola lettura

Il programma Streamlit locale, il repository `marketplacehub-1` e il sito Streamlit pubblico sono riferimenti. Non vengono modificati.

## D-002 — Ricostruzione isolata

La ricostruzione avviene su `rebuild/streamlit-parity-v2`. Il precedente stato è archiviato su
`archive/pre-rebuild-2026-09-07`. Dopo il rilascio B03 lo staging esistente segue il ramo rebuild.

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

## D-008 — Fondazione separata per processo

La fondazione usa Next.js/TypeScript per le superfici web e Python/FastAPI per conservare la portabilità delle logiche originali. API e worker sono processi diversi; PostgreSQL conserva i dati durevoli e Redis supporta coda, lock e stato temporaneo.

## D-009 — Identità separata dal tenant

L'autenticazione B03 stabilisce chi è l'utente e quale portale può aprire. Organizzazioni, membership, ruoli e seller scope appartengono al modello B04 e saranno sempre risolti dal backend. In questo modo una scelta del browser non può concedere accesso a un realm o a un tenant.

## D-010 — Continuità delle assegnazioni e scope a ogni richiesta

B04 importa le assegnazioni dal SaaS congelato `93cab535e89f36d8149c5298f70463d2c297c7ac`.
Il login valido non concede un tenant. La membership diretta prevale sulle deleghe Agency;
lo scope personale resta un'intersezione. I nomi e le autorizzazioni mostrati provengono dal
backend. Le funzioni di amministrazione delle assegnazioni e i controlli specifici dei moduli
restano nei rispettivi blocchi: le etichette dei permessi non rappresentano funzioni già pronte.
