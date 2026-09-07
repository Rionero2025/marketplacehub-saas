# Blocco 4 — organizzazioni, assegnazioni e negozio attivo

## Risultato

I tre portali leggono dal backend organizzazioni, negozi e autorizzazioni dell'utente autenticato.
Il pannello Seller mostra il nome interno del negozio, l'azienda, la ragione sociale e l'email
esistenti. Agency e Platform possono selezionare solo i Seller compresi nel proprio accesso.
La scelta persiste nella sessione e viene ricontrollata dal backend a ogni richiesta.

Questo blocco consegna la fondazione organizzativa e la consultazione delle anagrafiche.
Creazione/modifica di Seller e utenti, registrazione, abbonamenti e moduli operativi restano
pendenti. Nessuna formula contabile o di margine è stata modificata o sostituita.

## Fonti di compatibilità

- Streamlit locale versione 271: riferimento funzionale generale, precedente al modulo utenti.
- Streamlit GitHub `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`: `services/user_access.py`,
  `services/auth.py`, `services/session.py`, `pages/1_Gestione_Seller.py`.
- SaaS congelato `93cab535e89f36d8149c5298f70463d2c297c7ac`: `services/tenancy.py`,
  `services/user_access.py`, `api/dependencies.py`, `api/session_store.py`, `services/db.py`.
- Master Spec: organizzazioni PLATFORM/AGENCY/SELLER, ruoli, permessi e selezione Seller.

I repository originali e le loro tabelle operative rimangono in sola lettura.

## Migrazione 20260907_0004

Nove tabelle nuove: `organizations`, `organization_relationships`, `roles`, `permissions`,
`memberships`, `membership_permissions`, `seller_profiles`, `membership_seller_access`,
`workspace_selections`. I vincoli impediscono gerarchie invertite, auto-collegamenti e ruoli
di tipo incompatibile con l'organizzazione. Un Seller può appartenere direttamente alla
piattaforma; un'agenzia può avere più clienti.

La migrazione importa `tenants`, `tenant_memberships`, `tenant_sellers`, `agency_clients` e
`sellers`, usando i collegamenti utenti creati da B03. UUID deterministici mantengono la
corrispondenza con gli ID originali. Stati inattivi sono conservati; proprietà mancanti o
ambigue non vengono attribuite arbitrariamente e sono conteggiate nei log.
Il flag RLS legacy viene usato soltanto durante la lettura dei Seller e ripristinato nella
stessa transazione. Le definizioni della migrazione sono congelate, indipendenti dal futuro core.

Le percentuali originali di ripartizione restano nelle tabelle sorgenti. Il form completo di
Gestione Seller include quelle percentuali: sarà portato insieme alle sue validazioni, senza
anticipare un form parziale in questo blocco.

## Autorizzazione

- Il portale autenticato non crea da solo membership o proprietà di negozi.
- Membership, organizzazioni, relazioni e Seller devono essere attivi.
- Lo scope personale è sempre intersecato con proprietà e deleghe del tenant.
- Il selettore legacy nullo significa nessuna restrizione personale aggiuntiva; lista vuota
  o JSON invalido significano nessun Seller.
- Una membership diretta prevale su quella ereditata, anche quando è in sola lettura.
- Fra più deleghe viene selezionato il ruolo maggiore; Viewer e Platform Support non scrivono.
- Solo `is_admin=1` legacy concede amministrazione Platform globale. Owner/admin di tenant
  conservano i permessi originari e non ricevono privilegi globali.
- Identificativi estranei restituiscono 404; revoche rimuovono anche una selezione persistita.
- Due sessioni dello stesso utente hanno selezioni indipendenti.
- Il comando interno di bootstrap crea identità e membership Platform nella stessa transazione.

I permessi di menu originari sono mappati negli ambiti previsti dal Master Spec. Le etichette
visualizzate descrivono le assegnazioni; non sono moduli già implementati. Nel portare ciascun
modulo serviranno anche i controlli dell'operazione specifica e del piano. L'isolamento delle
future tabelle di dominio e dei job non viene dichiarato concluso da B04.

## API e interfaccia

- `GET /v1/workspace`: organizzazioni, Seller accessibili, scelta corrente e permessi.
- `POST /v1/workspace/select`: verifica lo scope prima di salvare la scelta nella sessione.
- `GET /v1/sellers/{seller_id}`: anagrafica soltanto entro lo scope autorizzato.
- BFF Next.js con cookie server-side e risposte senza cache; identificativi e DTO validati.
- Nessuna assegnazione e indisponibilità temporanea hanno stati distinti e recuperabili.
- Un errore di rete/503 nella verifica sessione non viene più presentato come logout;
  un 401/403 confermato continua a richiedere l'accesso corretto.
- Se la richiesta di uscita fallisce, il pannello mostra l'errore e permette di riprovare;
  il cookie viene rimosso solo dopo la revoca confermata della sessione.

## Verifiche e criteri

93 test Python superati: 42 precedenti, 25 di migrazione, 23 di autorizzazione e 3 del bootstrap
transazionale. Le prove di migrazione eseguono upgrade/downgrade Alembic su SQLite; la verifica
PostgreSQL reale avviene nel rilascio staging e va distinta dai test locali del ripristino RLS.
15 test Node verificano sessione, logout, BFF e fallimenti temporanei. Typecheck e build
di produzione Next.js completano la verifica frontend.

21 criteri del blocco: `MASTER-0058–0060`, `0062–0063`, `0140–0141`, `0143–0146`, `0501`,
`0602`, `0661–0666`, `0760–0761`. Ogni ID è registrato nel ledger e nell'indice generato.
Copertura totale: **70 / 2.017 = 3,47%**. Non è una stima delle ore residue né una dichiarazione
di parità delle funzioni operative Streamlit, che restano nei blocchi successivi.
