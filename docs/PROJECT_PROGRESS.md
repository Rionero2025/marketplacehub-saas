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

Calcolo: `26 / 2.017 = 1,289%`, mostrato con due decimali.

I 26 criteri soddisfatti sono i 18 output obbligatori della Fase 0 e gli 8 documenti permanenti richiesti dal Master Spec. I criteri di parità applicativa restano pendenti perché il ramo nuovo non contiene ancora il runtime.
