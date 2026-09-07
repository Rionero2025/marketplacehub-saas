# Moduli funzionali

## Superficie Streamlit misurata

| Pagina | Righe | Interazioni UI | Responsabilità |
|---|---:|---:|---|
| Dashboard | 1.248 | 4 | indicatori, alert, Top prodotti, navigazione operativa |
| Gestione Seller | 453 | 29 | Seller, account marketplace, regole e configurazioni |
| Fornitori e Listini | 706 | 38 | fornitori, feed, accessi, download e configurazione |
| Provider IA | 274 | 8 | profili IA, priorità, fallback, segreti e consumo |
| Assistenza Kaufland | 820 | 18 | ticket, messaggi, ordini collegati e azioni |
| Assistenza Marketplace | 1.174 | 23 | hub multicanale e bozze IA |
| Cancellazione Kaufland | 827 | 11 | scope, cache offerte e cancellazione massiva |
| Cancellazione Worten | 563 | 12 | offerte Worten e cancellazione massiva |
| Buy Box Kaufland | 2.788 | 18 | controlli, margine, azioni prezzo, storico |
| Buy Box Worten | 2.190 | 18 | controlli e azioni specifiche Worten |
| Creazione Prodotti | 1.607 | 59 | feed canonico, taxonomy, mapping, IA, validazione e pubblicazione |
| Lavora sui Listini | 344 | 12 | vista e selezione dei prodotti normalizzati |
| Ordini Kaufland | 1.605 | 12 | sincronizzazione, cache, filtri, dettagli e stati |
| Prodotti più venduti | 512 | 3 | aggregazioni e revisione prodotti |
| Pubblicazione Kaufland | 508 | 10 | selezione e invio offerte/prodotti |
| Pubblicazione Worten | 231 | 9 | preparazione e invio Mirakl/Worten |
| Contabilità | 2.237 | 35 | costi, commissioni, margini, override, Excel e PDF |
| Ordini Cecotec | 1.085 | 7 | match prodotti e file ordine ufficiale |
| Ordini Innpro | 553 | 8 | riconoscimento, export e storico |
| Packlink PRO | 3.357 | 27 | recupero ordini, colli, tariffe, creazione spedizioni e CSV |
| Tracciabilità | 1.831 | 15 | import tracking, match ordini, regole e invio |
| Storico | 33 | 2 | selezione dello storico operativo |
| Backup e Trasferimento | 203 | 7 | export/import dati applicativi |
| Database | 89 | 2 | configurazione e verifica backend dati |

Le tre pagine router Marketplace aggiungono la scelta del canale e inoltrano alle implementazioni specifiche.

## Servizi di dominio rilevati

- Dati e sessione: `db`, `postgresql_backend`, `database_config`, `session`, `security`, `data_transfer`.
- Marketplace Kaufland: client API, ordini, offerte, inventario, Buy Box, margine, supporto, storico e cancellazione.
- Marketplace Worten/Mirakl: client, pubblicazione, Buy Box, tracking e supporto.
- Fornitori: Cecotec, Innpro, AB Online e ActiveShop; altre compatibilità sono esercitate dai test della versione locale.
- Catalog intelligence: normalizzazione, provenienza, taxonomy, schema categorie, classificazione, mapping, IA, validazione, feed e pubblicazione.
- Operations: Packlink, tracking, scadenze di spedizione, selezione ordini e stati marketplace.
- Finance: contabilità, PDF, costi ordine, profit sharing, settlement e statistiche prodotto.
- Assistenza: connettori, thread, messaggi, azioni e bozze IA.

## Regola di completamento

Un modulo SaaS è equivalente soltanto quando il flusso completo replica il comportamento originale. La presenza di un endpoint o di una tabella con lo stesso nome non basta.
