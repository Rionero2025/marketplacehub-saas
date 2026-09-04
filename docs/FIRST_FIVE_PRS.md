# Prime cinque PR — proposta eseguibile

Stato: pianificate, non implementate e non pubblicate. Ogni PR aggiorna il registro delle differenze e mantiene il funzionamento Streamlit. Usare dati sintetici o anonimizzati; nessuna chiave API nei test.

## PR 1 — Rendere visibile la regressione nei test

Problema: CI compila solo parte del codice e non esegue `tests_core`. Il test onboarding v320 dipende dal nome di una funzione interna.

Ambito: workflow CI e test onboarding. Sostituire l'asserzione testuale con una prova della catena utente/tenant/seller/membership tramite le fixture esistenti. Eseguire `tests_core` in CI e includere API/core nel controllo di compilazione. Classificare i 116 test locali senza importarli tutti in questa PR.

Accettazione: suite verde, e un guasto intenzionale al collegamento seller-tenant fa fallire il test pertinente. Nessuna modifica alle regole onboarding necessaria solo per soddisfare il vecchio nome.

Migrazioni: nessuna. Rollback: ripristino workflow/test. Evidenza audit: ISSUE-01 e ISSUE-16.

## PR 2 — Conservare il recupero dello SKU composito Worten

Problema: due helper del servizio contabilità originale e i relativi chiamanti sono assenti nel SaaS.

Ambito: confronto dei due algoritmi, fixture e adattamento minimo in `services/accounting.py`. Riprodurre prima il caso con SKU archiviato incompleto e SKU composito valido nel raw JSON. Riutilizzare la precedenza originale fra dato archiviato valido e recupero dal payload; preservare correzioni manuali e comportamento degli altri marketplace.

Accettazione: casi con SKU completo, incompleto, JSON invalido, nessun candidato valido e override manuale. Parità di costo/margine tra implementazione originale e adattata sui casi applicabili; nessun ricalcolo massivo al deploy.

Migrazioni: nessuna automatica sui dati. Un eventuale ricalcolo storico sarà un'operazione separata con anteprima e conteggi. Rollback: revert del codice; le fixture restano documentazione del risultato atteso. Evidenza: ISSUE-02.

## PR 3 — Allineare membership, permessi e tenant delle route amministrative

Problema: un ruolo membership non limita necessariamente i permessi globali dell'utente; il tenant richiesto da alcune route può divergere dallo scope attivo.

Ambito: definire e testare il contratto per i ruoli già esistenti. Limitare l'intervento alla risoluzione dei permessi correnti e alle route tenant/billing che accettano un tenant esplicito. Nessuna nuova dashboard o gerarchia commerciale in questa PR. Se le due correzioni risultano indipendenti nel diff, separarle mantenendo la stessa milestone.

Accettazione su PostgreSQL: stesso utente owner in A e viewer in B; viewer non scrive in B; tenant estraneo rifiutato; Agency vede solo clienti assegnati; accesso amministrativo esplicitamente autorizzato usa lo scope corretto; worker mantiene isolamento. Verificare le query reali con RLS attivo, senza bypass come sostituto del test.

Migrazioni: non assumere necessità di schema prima della riproduzione. Rollback: revert della patch; conservare i test di isolamento. Se un difetto viene confermato, non riaprire il percorso vulnerabile come semplice rimedio operativo. Evidenza: ISSUE-03 e ISSUE-04.

## PR 4 — Deduplicare l'avvio della sincronizzazione ordini

Problema: due click o richieste concorrenti possono creare due job equivalenti; `SKIP LOCKED` protegge solo il claim del singolo job.

Ambito: ordini soltanto. Definire una chiave canonica comprendente tenant, seller/account, marketplace e parametri effettivi della sync; garantire atomicamente un solo job queued/running per chiave e restituire quello esistente. Non usare un lock globale. Mantenere distinta una richiesta con parametri diversi.

Accettazione: due connessioni PostgreSQL concorrenti producono un solo job attivo; tenant/account diversi procedono separatamente; job concluso o fallito permette una nuova richiesta; UI gestisce il job riutilizzato. Nessuna chiamata live necessaria nei test.

Migrazioni: eventuale chiave/indice versionato, dopo controllo dei duplicati attivi esistenti; evitare cancellazioni automatiche. Rollback: disabilitare il nuovo percorso e rimuovere il vincolo con migrazione reversibile se introdotto. Il recupero degli orfani resta PR distinta: non è risolto dalla deduplica. Evidenza: ISSUE-05.

## PR 5 — Preservare il catalogo valido durante la materializzazione

Problema: eliminazione iniziale e commit per blocchi espongono un catalogo incompleto dopo un errore.

Ambito: materializzazione catalogo e test di fault injection. Preparare i nuovi dati separatamente e attivarli atomicamente solo dopo completamento e validazione. Scegliere staging/versione o transazione compatibile con i volumi misurati; evitare di caricare tutto in memoria. Conservare i parser e le trasformazioni originali.

Accettazione: errore dopo il primo blocco lascia leggibile la versione precedente; import riuscito rende disponibile l'intero nuovo insieme; tenant diversi isolati; import concorrenti sullo stesso catalogo serializzati o rifiutati esplicitamente; verifica del caso catalogo vuoto secondo semantica originale. Cleanup non elimina la versione attiva.

Migrazioni: solo se necessarie alla soluzione scelta, additive e versionate. Rollback: versione precedente ancora disponibile; non ripristinare il vecchio comportamento distruttivo sui job in corso. Evidenza: ISSUE-06.

## Interventi successivi e gate di rilascio

Recupero sicuro dei job orfani, riconciliazione storage Render e protezioni autenticazione restano prioritari. Prima delle ottimizzazioni raccogliere baseline ripetibili; prima del porting UI scegliere componenti condivisi aderenti al design system. La parità completa dei 28 moduli richiederà altre PR: queste cinque stabilizzano le fondamenta e non la dichiarano raggiunta.
