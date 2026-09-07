# B10.2 — Collega marketplace nel Seller Enterprise

## Perimetro

Decisioni D-014 e D-015: sezione dedicata `/seller/marketplaces`, catalogo grafico multicanale,
ricerca e filtri, configurazione e verifica API, account collegati del Seller attivo. Il
percorso operativo è Seller → marketplace → account; non esiste un marketplace obbligatorio.
Il catalogo iniziale contiene 28 nomi, con Kaufland/Worten disponibili per il collegamento e
gli altri esplicitamente da sviluppare. Nessuna restrizione commerciale di piano in questo
blocco. Nessuna nuova funzione Agency o Platform, nessuna ripartizione utili.

## Fonti in sola lettura

- Streamlit locale v271 e checkout GitHub `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`:
  `pages/1_Gestione_Seller.py:95–231`, `services/kaufland.py:48–91,315`,
  `services/worten.py:1304`, `services/security.py`.
- [Kaufland REST API](https://sellerapi.kaufland.com/?page=rest-api) e
  [OpenAPI ufficiale](https://sellerapi.kaufland.com/swagger.json).
- [Mirakl A01 nel SDK ufficiale](https://raw.githubusercontent.com/mirakl/sdk-php-shop/master/src/Mirakl/MMP/Shop/Request/Offer/GetAccountRequest.php),
  [modello Shop](https://raw.githubusercontent.com/mirakl/sdk-php-shop/master/src/Mirakl/MMP/Common/Domain/Shop/AbstractShop.php),
  [ShopApiClientTrait](https://raw.githubusercontent.com/mirakl/sdk-php-shop/master/src/Mirakl/Core/Client/ShopApiClientTrait.php).
- [Catalogo Base.com](https://base.com/it-IT/integrazioni/) come riferimento di ricerca,
  filtri e schede. Provenienza grafica: `docs/reference/marketplace-brand-assets.json`.

## Matrice input → trasformazione → persistenza → output

| Operazione | Regola e risultato |
|---|---|
| Apri sezione | Sessione Seller e negozio assegnato; account circoscritti a Seller e organizzazione. Nessun conto o vendite simulati. |
| Cerca/filtra catalogo | Stato disponibile/da sviluppare distinto dal collegamento del singolo account. Marketplace da sviluppare non accetta chiavi. |
| Collega Kaufland | Nome account, Client Key, Secret Key trim; entrambe richieste per firma HMAC SHA256 di METHOD, URL completo, body vuoto, timestamp separati da newline. GET autenticato `/v2/info/storefront`. Nessuna scrittura marketplace. |
| Metadata Kaufland | Parser originale stringhe/oggetti, lowercase, trim, deduplica; storefront creati dal Seller a prescindere dallo stato. Nessun nome pubblico/ID Seller inventato. |
| Collega Worten | API Key e Shop ID trim; URL ufficiale `https://marketplace.worten.pt/api`. GET `/offers?shop_id=…&max=1`, header Authorization con chiave senza Bearer. Anche zero offerte è valido. |
| Metadata Worten | Tentativo A01 `/account?shop_id=…`; solo nome pubblico e ID reale consentiti; verifica corrispondenza ID. Dati KYC, banca e contatti non sono importati. |
| Salva collegamento | Solo dopo verifica riuscita; formato JSON cifrato Fernet compatibile con chiave master esistente; unicità Seller/marketplace/nome. |
| Account precedente | Conserva UUID, cifratura e nome. Inizialmente da verificare, senza necessità di reinserire le chiavi. |
| Verifica account salvato | Decifra solo lato backend; usa connettore del marketplace; aggiorna esito e orario senza eliminare le chiavi in caso di errore. |
| Rimuovi account | Conferma esatta ELIMINA, controllo account/Seller/organizzazione. Il blocco non cancella dati presso il marketplace. |
| Errore | Errori con codici controllati; nessun body remoto o segreto restituito. Timeout, permessi insufficienti e chiavi rifiutate distinti; nessuna scadenza dedotta da un semplice 401. |

## Vincoli applicativi

Ogni scrittura verifica `WORKSPACE_MANAGE`, anche dopo la chiamata remota prima di persistere.
Host fissi, redirect disabilitati, tempo massimo complessivo e limite risposta proteggono
le chiamate autenticate. Le API non accettano endpoint arbitrari né inoltrano credenziali a
marketplace non disponibili. Le risposte pubbliche sono allowlisted. Il BFF applica cookie,
origine, timeout, `no-store` e redazione degli errori. Il form svuota chiavi su cambio/chiusura,
salvataggio e sessione scaduta; scritture incerte richiedono rilettura prima di ripetere.

La verifica è conservata in una chiave dedicata di `settings_json` della tabella account
esistente, senza sovrascrivere le altre impostazioni. Non richiede migrazioni distruttive.
Gli endpoint storici Kaufland restano compatibili: un semplice salvataggio precedente non
equivale a connessione verificata; i nuovi collegamenti Seller usano il flusso generalizzato.

## Limiti espliciti

Questo blocco non implementa sincronizzazione ordini, pubblicazione prodotti, inventario,
contabilità o abbonamenti. Non dichiara connettori pronti per i 26 marketplace futuri.
L'integrazione commerciale Kaufland come fornitore tecnologico richiede inoltre la relativa
configurazione partner documentata dal provider: non si inventano chiavi partner.

## Accettazione

Test delle firme e dei parser, zero offerte, errore remoto senza salvataggio, cifratura,
isolamento, revoca durante la verifica, account importati, metadata, ri-verifica e rimozione.
Frontend: catalogo/filtri, richieste e risposte sicure, sessione, permessi e reset credenziali.
Collaudo browser con autenticazione e persistenza reali su fixture locali; le API remote
simulate sono dichiarate nel collaudo. Verifica pubblicazione sul commit esatto e health.
I criteri completati e i risultati finali sono registrati nel ledger e nel report di rilascio.

Verifiche concluse: **181 test Python** (47 nuovi per connessioni), **52 test frontend**,
typecheck, build produzione e Ruff. Browser locale con autenticazione/SQL reali e transport
marketplace simulato dichiarato: account Kaufland importato da verificare → collegato,
ricerca Worten, errore credenziali senza nuovo account, collegamento Worten con zero offerte
e nome/ID restituiti, riverifica, ricaricamento persistente e secondo Seller senza account.
Layout desktop e mobile 390 px verificati. Nessuna credenziale reale usata nei test locali.

8 criteri aggiunti: `LEGACY-UI-0015`, `0016`, `0017`, `0021`, `0022`,
`LEGACY-TEST-0323`, `MASTER-0521`, `0522`. Totale **87/2.011 = 4,33%**.
`MASTER-0520` resta parziale: sono presenti connected/unverified/error e cause specifiche,
ma non viene inventato lo stato expired senza un segnale del provider. Configurazione
generica degli altri canali e URL Worten libero non vengono conteggiati come completi.
