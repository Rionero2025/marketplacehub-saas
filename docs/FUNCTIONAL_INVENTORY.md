# Inventario funzionale vincolante

## Sorgenti dell'inventario

L'inventario combina tre insiemi senza dichiarare completata una funzione per semplice somiglianza:

1. 703 casi di test della versione Streamlit 271;
2. 391 interazioni UI Streamlit, identificate per file e riga;
3. 923 obblighi numerati o puntati nel Master Spec.

Il totale è **2.017 criteri di accettazione**. Il file `docs/reference/project-acceptance-index.json` contiene ogni criterio con ID, origine, testo e stato.

D-013 esclude 6 criteri di ripartizione utili dalla baseline: il perimetro SaaS attivo è
**2.011 criteri**. I criteri misti restano richiesti senza quote nostro/partner. Le esclusioni
e i requisiti effettivi sono in `docs/progress/scope-overrides.json`.

## Blocchi di consegna

| Blocco | Contenuto |
|---|---|
| B01 | audit, baseline, contratto di parità, inventario e misura |
| B02 | workspace, toolchain, core/API/web/worker e pipeline di test |
| B03 | autenticazione e sessioni Seller, Agency e Platform |
| B04 | organizzazioni, membership, ruoli, permessi e tenant isolation |
| B05 | sito pubblico, prezzi Free/Bronze/Silver/Gold/Platinum/Enterprise e trial |
| B06 | Stripe, sottoscrizioni, webhook, entitlement e fatturazione |
| B07 | registrazione e onboarding self-service |
| B08 | dashboard e strumenti Platform Admin |
| B09 | dashboard Agency, Seller figli, collaboratori e aggregati |
| B10 | configurazione Seller, utenti, marketplace e regole commerciali |
| B11 | dashboard Seller e indicatori operativi |
| B12 | fornitori, listini, feed, valute, accessi e download |
| B13 | lavorazione listini, normalizzazione e viste salvate |
| B14 | provider IA, segreti, priorità, fallback e utilizzo |
| B15 | creazione prodotti, taxonomy, mapping, traduzioni, IA e validazione |
| B16 | pubblicazione Kaufland |
| B17 | pubblicazione Worten/Mirakl |
| B18 | Buy Box e pricing Kaufland |
| B19 | Buy Box e pricing Worten |
| B20 | ordini Kaufland e cache incrementale |
| B21 | ordini multicanale, stati, filtri e prodotti più venduti |
| B22 | ordini fornitore Cecotec |
| B23 | ordini fornitore Innpro |
| B24 | Packlink PRO, tariffe, colli, spedizioni e CSV |
| B25 | tracking, import, match, regole e invio marketplace |
| B26 | contabilità completa, costi, margini, override, Excel e PDF |
| B27 | settlement, pagamenti, rettifiche e rimborsi |
| B28 | ticket, messaggi, bozze IA e assistenza |
| B29 | cancellazioni, storico operativo e audit |
| B30 | backup, trasferimento, restore e amministrazione database |
| B31 | job, cache, performance, osservabilità e sicurezza |
| B32 | design system, accessibilità, responsive e coerenza delle tre aree |
| B33 | staging, confronto parità, hardening e passaggio controllato in produzione |

Ogni blocco successivo deve indicare gli ID passati da `pending` a `verified`, i test eseguiti e l'evidenza di staging. Un criterio può diventare `verified` soltanto con implementazione e prova equivalente.

## Dettaglio obbligatorio della Contabilità

Il blocco B26 include espressamente nome prodotto, EAN, SKU e SKU composito, fornitore, listino contabile selezionato, costo d'acquisto, prezzo di vendita, commissione, rimborso, payout/da ricevere, costo extra, quantità, margine, utile del Seller, numero ordine fornitore, cliente, tracking, ricevuta e note.

La ripartizione nostro/partner è esclusa dal SaaS per richiesta esplicita dell'utente
(D-013). Nessun portale deve proporre percentuali o quote di ripartizione.

Deve inoltre replicare selezione persistente per riga, selezione/deselezione dei filtrati, azzeramento, editing diretto, salvataggio immediato, righe economiche bloccate per stati annullati/rimborsati/resi/no stock, import/confronto Excel, prevenzione export duplicati, storico export e PDF per periodo. Questa descrizione impedisce che una semplice tabella ordini venga chiamata “Contabilità”.
