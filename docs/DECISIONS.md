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

## D-011 — Dashboard ispirata a Base.com

Il 7 settembre 2026 l’utente ha richiesto una dashboard molto simile a Base.com. La superficie
B04 adotta una barra scura con icone, un secondo menu chiaro, una testata compatta e pannelli
bianchi con tabelle dense. Il marchio resta Marketplace Hub. Seller, Agency e Platform condividono
questi componenti. Sono presenti solo collegamenti a sezioni disponibili e conteggi restituiti
dal workspace; non vengono introdotte metriche commerciali simulate.

Riferimenti visivi pubblici: [lista ordini](https://www.base.com/en-EN/help/knowledgebase/order-list-how-to-use/)
e [accesso rapido](https://base.com/en-EN/blog/quick-access-to-your-favorite-features-in-baselinker/).
Le immagini pubblicate tra 2020 e 2024 sono riferimenti di impaginazione, non una verifica della
dashboard privata corrente di Base.com. Il cambio grafico non modifica API, scope, formule o
la fonte originale Streamlit e non incrementa da solo la copertura funzionale del progetto.

## D-012 — Primo blocco operativo Seller anticipato

Dopo B04 l'utente ha scelto di proseguire con anagrafica, percentuali e account Kaufland.
B10.1 viene quindi anticipato rispetto a B05 (sito e pacchetti), che rimane pendente.
L'edit di nome/ragione sociale/email è l'estensione SaaS concordata; regole delle percentuali,
cifratura e gestione Kaufland seguono l'originale. Il salvataggio credenziali non viene
presentato come verifica della connessione. La sincronizzazione ordini sarà il passo operativo
successivo, senza dichiararla completata da questa configurazione.

## D-013 — Esclusione della ripartizione degli utili dal SaaS

Il 7 settembre 2026 l'utente ha chiarito: «nel programma saas la ripartizione utili margini
non è necessaria». Questa decisione sostituisce la parte di D-012 sulle percentuali e prevale
sulla parità Streamlit e sui riferimenti del Master Spec per questa sola funzione.

Seller, Agency e Platform non configurano percentuali nostro/partner e non calcolano né
mostrano quote di utile ripartite. Restano richiesti costi, ricavi, commissioni, margine e
utile del singolo Seller, oltre agli aggregati autorizzati. Le funzioni contabili non ancora
trasferite rimangono pendenti. Gli abbonamenti SaaS non cambiano.

L'anagrafica salva soltanto nome, ragione sociale ed email. I vecchi campi non sono esposti
dalle API né letti o aggiornati dai servizi applicativi. Le migrazioni già pubblicate e i
dati storici importati rimangono conservati e inerti, senza cancellazioni. Il codice e i
dati originali Streamlit restano in sola lettura.

I criteri esclusivamente relativi alla ripartizione sono esclusi dal perimetro attivo con
motivazione verificabile; i criteri misti restano richiesti per tutte le altre operazioni.

## D-014 — Priorità al Seller Enterprise completo

L'utente ha stabilito che il lavoro attuale riguarda esclusivamente il pannello Seller nella
versione funzionalmente completa Enterprise. Si completano i moduli senza restrizioni di
pacchetto; soltanto dopo si definiranno abilitazioni e limiti per i pacchetti acquistati,
applicati anche dal backend. Questa scelta non attribuisce acquisti o abbonamenti fittizi.
Le nuove funzioni Agency e Platform restano rinviate; isolamento dei dati e permessi
esistenti continuano ad applicarsi. D-013 resta valida.

## D-015 — Collega marketplace precede la sincronizzazione ordini

L'utente richiede una sezione autonoma nel Seller con una griglia grafica di marketplace,
ispirata a Base.com. L'account collegato a monte determina il connettore usato dai moduli
successivi: nessun flusso ordini o contabile deve essere fisso su Kaufland.

Il primo catalogo comprende 28 marketplace riconoscibili, con ricerca e stato esplicito.
Kaufland e Worten hanno connettori di verifica API in questo blocco. Gli altri sono
segnalati come da sviluppare e non raccolgono credenziali. La presenza nella griglia non
implica che sincronizzazione ordini, catalogo o contabilità siano già implementati.

Il nuovo flusso Verifica e collega richiede credenziali complete e verifica remota prima
del salvataggio cifrato: è un'estensione richiesta rispetto al semplice salvataggio
Streamlit. Conserva nome account, trim, cifratura, isolamento e conferma ELIMINA. L'URL
Worten è limitato al servizio ufficiale, con redirect disabilitati. Questo protegge le
credenziali in un servizio multiutente senza introdurre endpoint inventati.

Metadata mostrati soltanto se restituiti dalle API: Kaufland espone storefront registrati,
non necessariamente attivi, e non un nome negozio pubblico nel contratto consultato.
Worten può restituire Shop ID e nome pubblico tramite A01. Credenziali già importate sono
da verificare; una risposta 401 non viene arbitrariamente interpretata come scadenza.
