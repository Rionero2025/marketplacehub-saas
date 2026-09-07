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

### Correzione della continuità degli account esistenti

Il primo rilascio B03 creava le nuove tabelle senza trasferire gli utenti di `app_users`; inoltre
il verificatore accettava solo Argon2, mentre il SaaS precedente salvava PBKDF2. Il controllo online
con un utente inesistente verificava il collegamento API, ma non dimostrava che un cliente esistente
potesse entrare. Il rifiuto del login di Rionero ha evidenziato questa verifica mancante.

La migrazione `20260907_0003` trasferisce le identità con UUID stabili e mantiene il collegamento
all'ID precedente in `auth_legacy_user_links`. Copia hash e stato; riproduce i portali consentiti da
tenant attivi, membership attive e deleghe agenzia attive. Solo l'indicatore globale `is_admin=1`
autorizza Platform: i ruoli tenant `owner` e `admin` non lo fanno. Collisioni dei login normalizzati
interrompono la migrazione senza unire account. Le tabelle precedenti non vengono modificate.

Il verificatore legge il formato PBKDF2 esatto del vecchio SaaS. Dopo un login valido aggiorna
l'hash ad Argon2id con un confronto atomico che protegge dai cambi password concorrenti. La policy
nuove password non viene applicata retroattivamente a una password esistente già verificata.
Password errate, account disattivi e portali non autorizzati non provocano aggiornamenti.
Il downgrade conserva gli account e le password già aggiornate; rimuove solo la tabella di
collegamento, che un successivo upgrade può ricostruire.

Questa correzione appartiene a B03 e non aggiunge criteri al totale del progetto.

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

- 42 test Python superati, di cui 22 sulla migrazione e sull'accesso con credenziali precedenti;
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
