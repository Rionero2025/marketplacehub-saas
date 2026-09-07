# Avanzamento del progetto

## Formula

`percentuale = criteri con stato verified / criteri nel perimetro attivo × 100`

Il denominatore è generato e controllabile in `docs/reference/project-acceptance-index.json`:

- 703 regressioni originali;
- 391 interazioni originali;
- 923 requisiti del Master Spec.

Questa percentuale misura la copertura verificata del prodotto finale. Non è una stima delle ore residue. Un elemento documentato ma non implementato resta `pending`.

La baseline originale resta di 2.017 criteri. D-013 esclude 6 criteri esclusivamente relativi
alla ripartizione degli utili: il perimetro attivo è **2.011**. I criteri misti mantengono le
verifiche su costi, margini, export e dashboard. Gli override espliciti sono riproducibili in
`docs/progress/scope-overrides.json`; il generatore li applica senza alterare gli originali.

## Stato corrente

| Blocco | Stato | Criteri verificati nel blocco | Progetto totale |
|---|---|---:|---:|
| B01 — Audit e inventario | completato | 26 | **1,29%** |
| B02 — Fondazione tecnica | completato | 8 | **1,69%** |
| B03 — Autenticazione e accessi | completato | 15 | **2,43%** |
| B04 — Organizzazioni e negozio attivo | completato | 21 | **3,47%** |
| B10.1 — Anagrafica e account Kaufland | completato; B10 resta parziale | 9 | **3,93%** |

Calcolo corrente: `79 / 2.011 = 3,928%`, mostrato con due decimali. I totali B01–B04 nella
tabella sono quelli storici al rilascio. La variazione rispetto a 3,97% deriva esclusivamente
dal nuovo perimetro: dei 6 criteri esclusi, uno era verificato e cinque pendenti. La rimozione
non viene conteggiata come nuova funzione completata.

B10.1 è stato anticipato su richiesta dell'utente per dare operatività al Seller. Sito/piani,
billing e onboarding B05–B07 restano pendenti. Il blocco trasferisce configurazione e
salvataggio credenziali Kaufland; connessione API e sincronizzazione ordini non sono incluse.
I 9 ID attivi sono elencati nel ledger con evidenza `docs/blocks/B10_1_SELLER_SETTINGS.md`.

Correzione D-013: rimossi campi, DTO, validazioni, normalizzazione e letture/scritture delle
quote. Salvataggio anagrafica indipendente dalle percentuali; dati importati conservati.
Verifiche della correzione: 29 test API/core, 36 test frontend, typecheck e build produzione.

Rifinitura grafica B04 del 7 settembre 2026: dashboard ispirata a Base.com, con navigazione
compatta, pannelli e tabella autorizzazioni responsive. Nessun criterio operativo aggiuntivo:
la copertura funzionale resta **70/2.017 (3,47%)**. Vedere decisione D-011.

Correzione B03 del 7 settembre 2026: trasferimento delle identità precedenti e compatibilità con
le password PBKDF2, mancanti nel primo rilascio. Il controllo iniziale di rifiuto credenziali non
verificava l'accesso di un account esistente. La correzione non incrementa la copertura del prodotto;
il totale resta 49/2.017. Le funzioni operative del Seller restano da ricostruire.

I primi 26 criteri soddisfatti sono gli output della Fase 0 e i documenti permanenti. B02 aggiunge
la fondazione eseguibile. B03 aggiunge autenticazione e sessioni. B04 aggiunge organizzazioni,
membership, autorizzazione backend e selezione del negozio con i dati precedenti. Gestione completa
di utenti/Seller, abbonamenti e altri moduli operativi Streamlit restano pendenti.
