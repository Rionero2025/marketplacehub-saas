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
| B03 — Autenticazione e accessi | completato | 15 | **2,43%** |
| B04 — Organizzazioni e negozio attivo | completato | 21 | **3,47%** |

Calcolo corrente: `70 / 2.017 = 3,471%`, mostrato con due decimali.

Correzione B03 del 7 settembre 2026: trasferimento delle identità precedenti e compatibilità con
le password PBKDF2, mancanti nel primo rilascio. Il controllo iniziale di rifiuto credenziali non
verificava l'accesso di un account esistente. La correzione non incrementa la copertura del prodotto;
il totale resta 49/2.017. Le funzioni operative del Seller restano da ricostruire.

I primi 26 criteri soddisfatti sono gli output della Fase 0 e i documenti permanenti. B02 aggiunge
la fondazione eseguibile. B03 aggiunge autenticazione e sessioni. B04 aggiunge organizzazioni,
membership, autorizzazione backend e selezione del negozio con i dati precedenti. Gestione completa
di utenti/Seller, abbonamenti e funzioni operative Streamlit restano pendenti.
