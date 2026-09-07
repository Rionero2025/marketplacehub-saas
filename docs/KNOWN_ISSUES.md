# Problemi e rischi noti

## Bloccanti per la trasformazione SaaS

1. Il modello originale seleziona un Seller in sessione, ma non implementa la gerarchia Platform/Agency/Seller né un controllo tenant uniforme nel backend.
2. La versione locale 271 e il repository cloud non coincidono: il cloud contiene moduli di autenticazione e deployment che non sono presenti nella stessa forma nella cartella locale.
3. Il ramo di ricostruzione è intenzionalmente privo di runtime: nessuna funzione del vecchio SaaS viene conteggiata come equivalente senza nuova verifica.
4. Non esistono ancora billing, entitlement, onboarding self-service e pannelli separati per Agency e Platform nel motore originale.

## Colli di bottiglia

- Pagine molto grandi orchestrano UI, rete, trasformazioni e persistenza: Packlink supera 3.300 righe, Buy Box Kaufland 2.700, Contabilità e Buy Box Worten 2.100.
- Il modello Streamlit riesegue la pagina e richiede cache e session state per evitare lavoro ripetuto.
- Varie chiamate HTTP e generazioni file sono sincrone; nel SaaS devono diventare job quando superano la durata di una richiesta web.
- Lo schema nasce da DDL distribuite nei servizi, rendendo più difficile prevedere e migrare lo stato.
- Tabelle e query dense richiedono paginazione server-side; caricare interi dataset nel browser non scala.

## Duplicazioni e accoppiamento

- `kaufland_live_units` e `kaufland_inventory_syncs` sono dichiarate sia nel DB centrale sia nel servizio inventario.
- Kaufland e Worten hanno pagine parallele per pubblicazione, Buy Box e cancellazione, con router molto piccoli sopra implementazioni separate.
- Formattazione, selezione, progress e gestione errori sono ripetute nelle pagine e devono diventare componenti condivisi senza cambiare i flussi.
- Alcune regole economiche sono calcolate anche nel JavaScript della griglia per l'anteprima immediata: il backend deve restare autorevole e produrre lo stesso valore.

## Sicurezza e affidabilità

- La chiave master unica protegge molte credenziali; la rotazione deve essere progettata prima della produzione SaaS.
- Route nascoste non possono proteggere l'area Platform: servono autorizzazioni backend e audit trail.
- Import da URL richiedono protezioni SSRF e limiti di dimensione; il modulo contabile contiene già controlli da preservare.
- Webhook e job devono essere idempotenti e riprendibili.
- Manca una prova completa di restore e disaster recovery.

## Dipendenze

`requirements.txt` usa in prevalenza limiti minimi senza lock completo. L'audit statico non certifica pacchetti obsoleti, ma rileva il rischio di build non riproducibili e un'immagine pesante dovuta a OCR, PDF, Excel, IA e storage nello stesso processo. Il nuovo repository deve separare dipendenze web/worker e bloccare versioni validate.

## Lacune di test

Mancano E2E browser, test di isolamento tenant, autorizzazioni per ruolo, billing/webhook, onboarding, concorrenza multiutente, load test e confronti completi originale/SaaS. Le 703 regressioni originali rimangono obbligatorie ma dovranno essere adattate al nuovo core senza indebolirne le aspettative.
