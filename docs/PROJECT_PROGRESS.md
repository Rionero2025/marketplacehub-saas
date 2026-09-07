# Avanzamento del progetto

## Formula

`percentuale = criteri con stato verified / 2.017 × 100`

Il denominatore è generato e controllabile in `docs/reference/project-acceptance-index.json`:

- 703 regressioni originali;
- 391 interazioni originali;
- 923 requisiti del Master Spec.

Questa percentuale misura la copertura verificata del prodotto finale. Non è una stima delle ore residue. Un elemento documentato ma non implementato resta `pending`.

## Stato corrente

| Blocco | Stato | Criteri verificati nel blocco | Progetto totale |
|---|---|---:|---:|
| B01 — Audit e inventario | completato | 26 | **1,29%** |
| B02 — Fondazione tecnica | completato | 8 | **1,69%** |

Calcolo corrente: `34 / 2.017 = 1,685%`, mostrato con due decimali.

I primi 26 criteri soddisfatti sono gli output della Fase 0 e i documenti permanenti. B02 aggiunge health check, migrazioni controllate e reversibili, ambienti configurabili, pool PostgreSQL, worker separato, build bloccate e controllo dei segreti. Le funzioni operative Streamlit restano pendenti.
