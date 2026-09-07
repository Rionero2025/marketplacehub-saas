# Blocco 3 — autenticazione e accessi distinti

## Risultato

Il SaaS espone due ingressi visibili, Seller e Agenzia, e un ingresso Platform separato in
`/system-admin/login`, non collegato dalle pagine pubbliche e marcato `noindex, nofollow`. Ogni
destinazione protetta verifica la sessione e il realm sul backend prima di mostrare il pannello.

Questo blocco autentica l'identità e il tipo di portale. Organizzazioni, membership, ruoli granulari
e isolamento dei dati saranno aggiunti nel B04; non sono simulati nel frontend.

## Backend e persistenza

- `auth_users` conserva identità, stato e hash password;
- `auth_user_realms` autorizza l'identità a Seller, Agency o Platform;
- `auth_sessions` conserva solo SHA-256 del token opaco, con scadenza, ultimo utilizzo e revoca;
- le password usano Argon2id e una policy minima verificata;
- il login restituisce sempre lo stesso errore per utente, password o realm errati;
- Redis limita i tentativi per coppia client/login senza salvare il login in chiaro nella chiave;
- cookie `HttpOnly`, `SameSite=Lax`, con `Secure` automatico in staging e produzione;
- CORS accetta soltanto le origini configurate;
- input API tipizzati e limitati; accesso SQL tramite SQLAlchemy e parametri;
- comando interattivo `marketplace-hub-create-platform-admin` senza password negli argomenti.

La migrazione `20260907_0002` è stata provata sia in upgrade sia in downgrade.

## Frontend

- `/` presenta soltanto Seller e Agenzia;
- `/login/seller` e `/login/agency` usano form dedicati;
- `/system-admin/login` è l'ingresso interno Platform;
- `/seller`, `/agency` e `/system-admin` sono destinazioni protette e separate;
- il browser parla con route BFF Next.js, mentre l'URL interno dell'API resta server-side;
- il token di sessione non entra nel JSON e non è leggibile da JavaScript.

Le tre destinazioni contengono intenzionalmente solo l'intestazione del pannello. Le funzioni di
dominio non vengono anticipate: il pannello Seller sarà ricostruito dai comportamenti Streamlit nei
blocchi successivi.

## Verifica

- 19 test Python superati;
- Ruff superato;
- typecheck TypeScript superato;
- build di produzione Next.js superata, con tutte le route previste;
- ciclo login, lettura sessione, logout, revoca e scadenza verificato;
- separazione realm, rate limit, cookie staging, input errati e repository SQL verificati;
- migrazione Alembic upgrade/downgrade verificata.

## Criteri chiusi

Il blocco chiude 15 criteri: `MASTER-0496`, `MASTER-0497`, `MASTER-0499`, `MASTER-0500`,
`MASTER-0507`, `MASTER-0508`, `MASTER-0509`, `MASTER-0510`, `MASTER-0512`, `MASTER-0513`,
`MASTER-0514`, `MASTER-0516`, `MASTER-0667`, `MASTER-0668`, `MASTER-0669`.

Totale progetto: `49 / 2.017 = 2,429%`, visualizzato come **2,43%**.
