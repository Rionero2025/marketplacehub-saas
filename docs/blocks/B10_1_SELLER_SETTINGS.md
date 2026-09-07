# B10.1 — Anagrafica e account Kaufland

## Perimetro concordato

Primo passo operativo della sezione Negozio: modifica anagrafica del Seller autorizzato,
aggiunta/elenco/eliminazione account Kaufland. La ripartizione degli utili, presente nel primo
rilascio, è stata esclusa per richiesta esplicita dell'utente (D-013). Il form e le API non
espongono né salvano percentuali nostro/partner; non servono per salvare l'anagrafica.
Il blocco B10 completo (creazione/attivazione/eliminazione Seller, collaboratori e altri
marketplace) resta parziale. Ordini e calcoli contabili appartengono ai blocchi successivi.

Fonti originali in sola lettura: versione locale 271 e GitHub `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`,
`pages/1_Gestione_Seller.py`, `services/profit_sharing.py`, `services/security.py`, `services/db.py`.
La migrazione dati usa le tabelle del SaaS congelato `93cab535e89f36d8149c5298f70463d2c297c7ac`.

## Matrice di parità

| Input/operazione | Regola originale e risultato nel SaaS |
|---|---|
| Seller attivo | Scelta del negozio autorizzato; letture e scritture circoscritte a Seller e organizzazione. |
| Ripartizione utili | Esclusa in tutti i portali per D-013. Nessuna percentuale richiesta, letta, calcolata o salvata dai servizi applicativi. Margine e utile del singolo Seller restano nel futuro blocco contabile. |
| Nome/ragione sociale/email | Nome non vuoto; trim dei tre campi, email libera come nell'originale. L'edit di questi campi è l'estensione SaaS concordata: l'originale li inseriva alla creazione, ma non li modificava nel form esistente. |
| Account Kaufland | Nome proposto Kaufland principale; nome trim e almeno una fra Client Key/Secret Key. Marketplace lowercase, account attivo, JSON cifrato Fernet e impostazioni iniziali vuote. |
| Elenco account | Ordinamento marketplace/nome account; stato attivo e Client Key mascherata con 8 pallini e ultimi 4 caratteri. Secret Key mai restituita. |
| Eliminazione | Conferma esatta ELIMINA, verificata anche dal backend; controllo account ID/Seller/organizzazione, eliminazione della sola riga del nuovo modello. Nessuna tabella operativa esiste ancora da cancellare in cascata. |
| Errore o risposta incerta | Messaggi senza segreti, nessun successo inventato. Aggiornamento esplicito prima di ripetere una scrittura non confermata. Cambio Seller distrugge bozze e campi chiave del precedente. |

L'originale salva Kaufland senza verificarne le API. Pertanto lo stato è **configurato**, non
connesso; nome pubblico e paesi non sono inventati. La verifica di connessione e metadata
richiesta dal Master Spec §27 resta pendente (`MASTER-0520`, `0521`, `0522`).

## Persistenza e migrazione

La migrazione storica `20260907_0005`, già pubblicata, crea `seller_commercial_settings` e
`seller_marketplace_accounts`. Ha importato i Seller già collegati da B04, le percentuali normalizzate secondo la lettura originale,
gli account Kaufland, lo stato attivo, gli ID legacy e `settings_json` invariato. Il ciphertext
viene copiato senza decifrarlo o cambiarlo. Gli account senza Seller assegnato sono esclusi e
conteggiati; gli altri canali restano nelle tabelle sorgenti per le rispettive migrazioni.
Duplicati ambigui dei nomi account fermano l'importazione anziché perdere dati.

Dopo D-013 la tabella delle percentuali è solo un archivio inerte. Lettura e salvataggio
dell'anagrafica non la interrogano né la aggiornano. Nessuna migrazione distruttiva e nessuna
modifica retroattiva alla migrazione pubblicata; gli account Kaufland restano operativi.

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

La prova include salvataggio/rilettura dell'anagrafica senza percentuali, assenza dei vecchi
campi dalle risposte, conservazione dei valori storici senza aggiornamento, sessione autenticata,
isolamento Seller/organizzazione, account estranei e ruoli revocati/sola lettura, cifratura
compatibile, conferma eliminazione, migrazione e conservazione dei dati sorgenti.
Il collaudo browser locale usa autenticazione reale e dati interamente dimostrativi.

Non vengono conteggiati come completati il form multicanale intero, l'attivazione/creazione
Seller o il test API marketplace. Le quote contabili sono escluse, non pendenti. Il dettaglio dei criteri
verificati è nel ledger `docs/progress/verified-criteria.json`.

Primo rilascio: 10 criteri verificati, 143 test Python e 36 test frontend, typecheck e build.
Il perimetro e i conteggi correnti dopo D-013 sono in `docs/PROJECT_PROGRESS.md` e nel ledger.
