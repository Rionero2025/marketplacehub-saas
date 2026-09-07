# B10.1 — Anagrafica, ripartizione e account Kaufland

## Perimetro concordato

Primo passo operativo della sezione Negozio: modifica anagrafica del Seller autorizzato,
percentuali di ripartizione del margine utile, aggiunta/elenco/eliminazione account Kaufland.
Il blocco B10 completo (creazione/attivazione/eliminazione Seller, collaboratori e altri
marketplace) resta parziale. Ordini e calcoli contabili appartengono ai blocchi successivi.

Fonti originali in sola lettura: versione locale 271 e GitHub `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`,
`pages/1_Gestione_Seller.py`, `services/profit_sharing.py`, `services/security.py`, `services/db.py`.
La migrazione dati usa le tabelle del SaaS congelato `93cab535e89f36d8149c5298f70463d2c297c7ac`.

## Matrice di parità

| Input/operazione | Regola originale e risultato nel SaaS |
|---|---|
| Seller attivo | Scelta del negozio autorizzato; letture e scritture circoscritte a Seller e organizzazione. |
| Percentuali lette | Fallback 0/100; clamp 0–100, arrotondamento a 4 decimali; somma fuori tolleranza corretta assegnando al partner 100 meno quota nostra. Nessun 35/65 imposto ai dati importati. |
| Percentuali salvate | Valori finiti 0–100; somma entro 0,01 da 100. Persistenza dei valori immessi, con le stesse regole di normalizzazione alla lettura. |
| Nome/ragione sociale/email | Nome non vuoto; trim dei tre campi, email libera come nell'originale. L'edit di questi campi è l'estensione SaaS concordata: l'originale li inseriva alla creazione, ma non li modificava nel form esistente. |
| Account Kaufland | Nome proposto Kaufland principale; nome trim e almeno una fra Client Key/Secret Key. Marketplace lowercase, account attivo, JSON cifrato Fernet e impostazioni iniziali vuote. |
| Elenco account | Ordinamento marketplace/nome account; stato attivo e Client Key mascherata con 8 pallini e ultimi 4 caratteri. Secret Key mai restituita. |
| Eliminazione | Conferma esatta ELIMINA, verificata anche dal backend; controllo account ID/Seller/organizzazione, eliminazione della sola riga del nuovo modello. Nessuna tabella operativa esiste ancora da cancellare in cascata. |
| Errore o risposta incerta | Messaggi senza segreti, nessun successo inventato. Aggiornamento esplicito prima di ripetere una scrittura non confermata. Cambio Seller distrugge bozze e campi chiave del precedente. |

L'originale salva Kaufland senza verificarne le API. Pertanto lo stato è **configurato**, non
connesso; nome pubblico e paesi non sono inventati. La verifica di connessione e metadata
richiesta dal Master Spec §27 resta pendente (`MASTER-0520`, `0521`, `0522`).

## Persistenza e migrazione

`20260907_0005` crea `seller_commercial_settings` e `seller_marketplace_accounts`.
Importa i Seller già collegati da B04, le percentuali normalizzate secondo la lettura originale,
gli account Kaufland, lo stato attivo, gli ID legacy e `settings_json` invariato. Il ciphertext
viene copiato senza decifrarlo o cambiarlo. Gli account senza Seller assegnato sono esclusi e
conteggiati; gli altri canali restano nelle tabelle sorgenti per le rispettive migrazioni.
Duplicati ambigui dei nomi account fermano l'importazione anziché perdere dati.

La lettura delle tabelle legacy RLS usa lo scope della migrazione nella stessa transazione e
lo ripristina anche in caso di errore. Le tabelle sorgenti non vengono aggiornate o cancellate.
Il downgrade rimuove esclusivamente le due nuove tabelle; usarlo dopo nuove scritture richiede
la conservazione preventiva dei dati nuovi.

La derivazione della chiave resta SHA256(master) → base64 URL-safe → Fernet. `MH_MASTER_KEY`
accetta anche l'alias `MARKETPLACE_HUB_MASTER_KEY` già usato sullo staging. Non sostituire il
valore esistente quando si importano credenziali cifrate. Una chiave assente impedisce nuove
scritture di credenziali con errore 503, senza impedire l'accesso all'app o all'anagrafica.

## API e permessi

- `GET /v1/sellers/{id}/settings`.
- `PUT /v1/sellers/{id}/settings`.
- `POST /v1/sellers/{id}/kaufland-accounts`.
- `DELETE /v1/sellers/{id}/kaufland-accounts/{account_id}`, body `confirmation: ELIMINA`.

Ogni mutazione richiede `WORKSPACE_MANAGE` in scrittura, verificato a ogni richiesta. Utenti
in sola lettura possono consultare i dati autorizzati. Il BFF ricostruisce esplicitamente il
DTO pubblico e rimuove campi aggiuntivi; ciphertext e chiavi complete non raggiungono la
risposta. Anche errori di validazione con JSON malformato sono redatti. Le chiavi inserite
esistono solo transitoriamente nel form e sono svuotate dopo il salvataggio.

## Accettazione

La prova include normalizzazione originale, salvataggio/rilettura, sessione autenticata,
isolamento Seller/organizzazione, account estranei e ruoli revocati/sola lettura, cifratura
compatibile, conferma eliminazione, migrazione e conservazione dei dati sorgenti.
Il collaudo browser locale usa autenticazione reale e dati interamente dimostrativi.

Non vengono conteggiati come completati il form multicanale intero, l'attivazione/creazione
Seller, il calcolo delle quote contabili o il test API marketplace. Il dettaglio dei criteri
verificati è nel ledger `docs/progress/verified-criteria.json`.

10 criteri: `LEGACY-UI-0011`, `0012`, `0013`, `0024`, `LEGACY-TEST-0599`,
`MASTER-0498`, `0517`, `0518`, `0519`, `0742`. Totale **80/2.017 = 3,97%**.
Verifiche automatiche: **143 test Python**, **36 test frontend**, typecheck e build Next.js.
