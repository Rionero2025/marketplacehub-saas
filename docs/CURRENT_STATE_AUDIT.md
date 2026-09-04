# Marketplace Hub — audit prima della migrazione completa

Data: 4 settembre 2026. Ambito: confronto sorgenti, copertura funzionale, architettura e proposta delle prime cinque PR. Nessuna funzionalità applicativa modificata durante questo audit.

## Esito

Il SaaS conserva gran parte del motore Python originale, ma non offre ancora tutte le sue funzioni tramite API e interfaccia web. Delle 28 aree Streamlit originali, **10 sono parzialmente disponibili e 18 non sono esposte come flusso completo nel SaaS**. Questi numeri descrivono aree funzionali, non percentuali di codice o di completamento.

La strategia proposta è riusare servizi e algoritmi esistenti, verificare i risultati con casi di regressione e collegare progressivamente core, API, worker e interfaccia. Non serve una riscrittura generale. La parità va dimostrata per ogni operazione, non dedotta dalla presenza del file Python.

Il design system concordato è una specifica da realizzare: oggi il frontend usa Next.js/React e CSS personalizzato; Tailwind e shadcn non risultano installati. Non sono presenti le tre dashboard distinte Platform, Agency e Seller.

## Sorgenti e limiti

| Sorgente | Versione esaminata | Ruolo |
|---|---|---|
| [SaaS](https://github.com/Rionero2025/marketplacehub-saas/tree/972e8f89fe980b9cac5ae3b5649857e8a0d15d59) | `972e8f89fe980b9cac5ae3b5649857e8a0d15d59` | Baseline del nuovo prodotto |
| [Originale](https://github.com/Rionero2025/marketplacehub-1/tree/4c3cda59387068f3dfb0f2bae45b7d03bf307dca) | `4c3cda59387068f3dfb0f2bae45b7d03bf307dca` | Il suo Blueprint chiama il servizio `marketplace-hub` |
| Installazione locale `Documents/marketplace_hub` | VERSION 271 | Confronto aggiuntivo di sorgenti e test |
| Master Spec allegato | Copia integrale nel pacchetto; SHA-256 in metadata | Requisiti dell'utente |

Inventario SaaS: 251 file di testo sorgente/configurazione/documentazione indicizzati, 178 file Python, 2.270 definizioni di funzione, 51 route API rilevate e 118 nomi di tabelle rilevati staticamente. Definizioni annidate e test sono inclusi nel conteggio dei simboli. Le tabelle non rappresentano una verifica dello schema live. File dati e credenziali non sono stati aperti per l'inventario.

Tutte le 28 pagine Streamlit del repository originale sono conservate nel SaaS, che ne contiene 29. Su 103 file Python originali, 71 hanno testo normalizzato identico. Questa misura non certifica il comportamento: i servizi modificati e i collegamenti frontend richiedono verifica specifica.

Il Master Spec è tracciato in 46 sezioni e 969 righe di sezioni/punti elenco. Le valutazioni dei punti non provati separatamente ereditano il giudizio conservativo della sezione. Non sono 969 funzionalità indipendenti né 969 test eseguiti. L'audit non equivale a una lettura manuale e a una prova completa di ogni riga di codice.

## Architettura utilizzabile

Il frontend Next.js chiama FastAPI; API e worker usano `marketplace_core` e i servizi Python condivisi con Streamlit. PostgreSQL conserva dati e job; Redis fornisce cache temporanea. Sono presenti sessioni persistenti, cifratura credenziali, tenant scope, RLS e una coda con claim PostgreSQL `SKIP LOCKED`.

Questo impianto consente un porting incrementale. La prima lacuna è l'esposizione dei flussi: contabilità mostra uno stato JSON, Buy Box un riepilogo JSON, cataloghi una lista; spedizioni, assistenza, pubblicazione e numerose azioni operative non hanno ancora l'interfaccia SaaS corrispondente.

Dettagli: [architettura](ARCHITECTURE.md), [database](DATABASE.md), [integrazioni](INTEGRATIONS.md), [moduli e parità](MODULES.md).

## Risultati prioritari

1. **Possibile regressione nel recupero costi Worten.** Nel servizio contabilità originale le funzioni `_best_composite_sku` e `_composite_sku_from_raw_json` recuperano lo SKU composito dal dato originale. Nel SaaS sono assenti. La differenza è confermata; l'impatto sui margini va riprodotto con fixture prima di correggere o ricalcolare dati.
2. **Ruoli organizzazione e contesto tenant da verificare insieme.** I permessi globali utente non costituiscono un RBAC completo per membership. Alcune route verso tenant diversi da quello attivo richiedono prove PostgreSQL sul contesto RLS. Sono rischi statici, non una fuga dati live dimostrata.
3. **Robustezza dei lavori asincroni incompleta.** Il claim evita che due worker prendano lo stesso job, ma non impedisce due richieste equivalenti. Non è stato rilevato un recupero automatico dei job `running` orfani.
4. **Import cataloghi non atomico.** La materializzazione elimina prima la versione precedente e salva a blocchi: un errore intermedio può lasciare un catalogo parziale. Occorre preservare l'ultima versione valida.
5. **Persistenza file da riconciliare con Render.** Il Blueprint indica storage locale con API e worker separati. I valori live non sono stati verificati: controllare condivisione e durata prima di dipendere dai file in produzione.
6. **Copertura test incompleta.** L'installazione locale contiene 116 file di test assenti nel SaaS. Vanno classificati e adattati, senza copiarli indiscriminatamente. Due moduli locali di localizzazione/enrichment sono assenti; non sono emersi chiamanti nelle pagine locali, quindi non sono dichiarati funzionalità attive perdute.

Elenco completo e distinzione fra fatti e ipotesi: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Verifiche disponibili

| Verifica | Risultato | Limite |
|---|---|---|
| Suite `tests_core`, baseline SaaS | **76 superati, 1 fallito** | Test eseguiti in ambiente locale; non matrice PostgreSQL multi-tenant |
| Test fallito onboarding v320 | Cerca nel sorgente `attach_seller`, sostituito da `_ensure_tenant_seller_link` | Test statico obsoleto; non dimostra che signup sia rotto |
| CI nel repository | Installazione dipendenze e compileall | Non esegue pytest né build frontend |
| Sincronizzazione Kaufland, verifica precedente a questo audit sulla stessa baseline | **1.000 unità lette, 1.000 salvate, zero errori** | Un'esecuzione reale; non certifica tutti gli stati o lo storico completo |
| Lettura archivio con scope tenant | Totale 1.000, pagina leggibile | Non sostituisce prove incrociate tra tenant |

Gli errori di propagazione contesto FastAPI e bootstrap subscription nel worker sono già stati corretti nei commit `26a6e57` e `972e8f8`. Non sono riproposti come problemi aperti. Nessun nuovo ordine, pagamento, invio o aggiornamento prezzi è stato eseguito durante questo audit.

Non sono stati eseguiti benchmark di carico, penetration test, prove di restore o test live di tutte le integrazioni. Non è dimostrato un miglioramento prestazionale di 5–10 volte.

## Prime cinque PR proposte

Le PR sono definite in [FIRST_FIVE_PRS.md](FIRST_FIVE_PRS.md), con ambito, verifiche e rollback. Non sono state aperte né implementate.

| Ordine | Risultato verificabile |
|---|---|
| 1 | CI esegue la suite; test onboarding verifica il comportamento e non un nome nel sorgente |
| 2 | Recupero SKU/costi Worten equivalente al legacy nei casi documentati |
| 3 | Autorizzazioni per membership e contesto tenant coerenti sulle route interessate |
| 4 | Richieste simultanee della stessa sincronizzazione ordini condividono un solo job attivo |
| 5 | Import catalogo fallito lascia disponibile l'ultima versione completa |

La sequenza mette prima la verifica e la conservazione dei risultati. Recupero job orfani, storage condiviso e protezioni autenticazione restano interventi prioritari prima di un lancio pubblico; non sono implicitamente inclusi nelle cinque PR.

## Dopo la stabilizzazione

Misurare tre percorsi prima di ottimizzarli: sincronizzazione Kaufland (chiamate remote e scritture per riga), dashboard (polling e richieste ripetute), import cataloghi (memoria e tempi per formato). Registrare durata, numero di richieste/query, memoria, quantità di dati e risultati prima/dopo. Conservare override manuali, isolamento tenant e algoritmi economici.

Poi costruire i componenti condivisi del design system e migrare ogni area per flusso completo: core verificato → API autorizzata → job dove necessario → interfaccia → prova di parità. Per l'avvio operativo: ordini, contabilità, cataloghi e Buy Box; seguono gli altri moduli inventariati. L'ordine non autorizza a eliminare le aree successive.

Platform, Agency e Seller devono usare lo stesso sistema di componenti con navigazione e permessi appropriati. Stripe reale, Fatture in Cloud e marketing sono lavoro ancora da implementare, non capacità già operative.

## Criterio di completamento

Una funzione è completata quando l'azione originale è raggiungibile nel SaaS, usa la logica preservata o un adattamento giustificato, produce risultati equivalenti su casi rappresentativi, rispetta permessi e tenant, mostra errori comprensibili e ha una verifica ripetibile. Un file presente o un pulsante visibile non bastano.

Questo pacchetto è la memoria documentale del lavoro: baseline, differenze, requisiti e decisioni sono salvati. Non è una promessa di memoria permanente della conversazione.


## Aggiornamento porting — 4 settembre 2026

Questo documento conserva la baseline del primo audit. Stato successivo: PR #1 integrata e CI superata; recupero SKU verificato sul ramo dedicato, in attesa di integrazione. Il conteggio corrente è in SAAS_PROGRESS.md; i numeri della baseline non vengono riscritti retroattivamente.


### Blocco 03 — verifica autorizzazioni

Stato successivo alla baseline: blocchi 01 e 02 integrati, 5% delle 40 milestone. Il blocco autorizzazioni è ancora in verifica. Vedere SAAS_PROGRESS.md.


### Blocco 04 — sincronizzazioni duplicate

Baseline storica invariata. Stato corrente: blocchi 01–03 integrati, 7,5%; blocco 04 in verifica. Worker Render osservato Live al commit de25a15 (blocco 02).


### Blocco 05 — cataloghi atomici

Stato corrente oltre la baseline: blocchi 01–04 integrati, 10%; blocco 05 in verifica. Vedere SAAS_PROGRESS.md.


### Blocco 06 — recupero job interrotti

Stato corrente oltre la baseline: blocchi 01–05 integrati, 12,5%; blocco 06 in verifica. Vedere SAAS_PROGRESS.md.


### Blocco 07 — integrità e storage condiviso

Stato corrente oltre la baseline: blocchi 01–06 integrati, 15%; blocco 07 parziale e non conteggiato. Worker Render verificato live sul commit 5b72c7f; deploy 12c447e avviato automaticamente. Vedere SAAS_PROGRESS.md.


### Hotfix — elenco cataloghi HTTP 500

Nuova evidenza live: errore cataloghi presente anche con elenco vuoto per incompatibilità SQL PostgreSQL. Hotfix in verifica; avanzamento invariato 6/40=15%, nessun nuovo blocco conteggiato.
