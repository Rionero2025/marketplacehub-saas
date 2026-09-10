# B12.1a — Fornitori e primo import dei listini

Data del contratto: 10 settembre 2026. Perimetro: pannello Seller Enterprise.
Il repository Streamlit originale, congelato al commit `7c8e5f9`, resta in sola
lettura.

Stato del documento: **CONTRATTO SORGENTE; IMPLEMENTAZIONE VERIFICATA LOCALMENTE,
COLLAUDO STAGING IN ATTESA**.

## Risultato osservabile richiesto

Il Seller deve poter registrare un fornitore, associargli un listino caricato dal
computer, verificare quante righe sono state lette, aprire un'anteprima normalizzata
ed eliminare in modo controllato il listino o il fornitore. Ogni dato deve appartenere
all'organizzazione e al Seller attivi e deve restare disponibile dopo riavvii e deploy.

Questa tranche crea la base durevole che i sottoblocchi successivi useranno per i feed
dei singoli fornitori e per il ricalcolo dei costi degli ordini. Non modifica ancora i
margini salvati nell'archivio Ordini.

## Matrice di parità

| Funzione | Input originale | Trasformazione originale | Persistenza originale | Output da riprodurre nel SaaS |
| --- | --- | --- | --- | --- |
| Registra fornitore | nome e note | spazi esterni rimossi; nome obbligatorio | `suppliers`, univoco per Seller | fornitore disponibile nel Seller attivo |
| Registra listino | fornitore, nome, origine e file | convalida fornitore/nome/file e rilevamento formato | `price_lists` e file locale | listino attivo con formato, data e numero prodotti |
| Legge file | CSV/TXT/TSV, XLS, XLSX o XML | lettura tabellare, intestazioni eterogenee | nessuna scrittura intermedia definitiva | righe pronte per la normalizzazione |
| Normalizza | colonne del fornitore | alias canonici, EAN/SKU testuali, numeri con virgola, valori mancanti coerenti | snapshot normalizzato | `ean`, `sku`, `name`, `cost`, `shipping_cost`, `total_cost`, `quantity` |
| Anteprima | listino accessibile | colonne canoniche in testa | nessuna | massimo 200 righe e conteggio completo |
| Elimina listino | proprietario + conferma `ELIMINA` | controllo ownership | cascata su righe e artefatto | listino rimosso senza toccare altri Seller |
| Elimina fornitore | proprietario + nome esatto | conteggio e cancellazione dei listini collegati | cascata atomica su fornitore, listini, righe e artefatti | esito esplicito e lista aggiornata |

Riferimenti principali nell'originale:

- `pages/2_Fornitori_e_Listini.py:113-307` per registrazione e import;
- `pages/2_Fornitori_e_Listini.py:309-358` per elenco e anteprima;
- `pages/2_Fornitori_e_Listini.py:653-706` per le eliminazioni;
- `services/lists.py:25-207` per salvataggio, download e riconoscimento formato;
- `services/lists.py:622-657` per la lettura;
- `services/lists.py:1093-1143` per la normalizzazione;
- `services/db.py:227-256` e `services/db.py:969-1107` per schema e cancellazioni.

## Regole congelate

- EAN e SKU sono identificatori testuali: gli zeri iniziali non devono essere persi.
- Le virgole decimali vengono interpretate come separatore decimale.
- Il costo totale è letto se presente; altrimenti deriva da costo e spedizione.
- Quantità invalide o negative non diventano stock positivo.
- Il file diventa la versione attiva soltanto dopo lettura e normalizzazione concluse.
- Un errore non deve lasciare un listino puntato a un file parziale.
- Il browser non può caricare PKL/pickle: nell'originale `read_pickle` esegue codice e
  non è adatto a input SaaS non fidato. La migrazione amministrativa di vecchi PKL sarà
  trattata separatamente senza modificare il repository originale.

## Adattamento architetturale obbligatorio

Il percorso locale `DATA_DIR/price_lists/<id>` non è durevole su Render. B12.1a salva
l'artefatto originale e le righe normalizzate in PostgreSQL, con hash SHA-256 e scope
`organization_id + seller_id`. Questa scelta conserva il risultato funzionale del
caricamento senza dipendere dal filesystem effimero.

Le letture richiedono il permesso `CATALOG`; creazione ed eliminazione richiedono anche
accesso in scrittura. Il `seller_id` inviato dal browser viene sempre verificato dal
backend tramite il workspace autorizzato. Nessuna query o chiave univoca può omettere lo
scope del Seller.

L'importazione accetta al massimo 20 MiB in questa tranche. Il supporto a listini molto
grandi, URL, credenziali feed, versionamento remoto e avanzamento asincrono appartiene a
B12.1b e non viene dichiarato completato qui.

## Casi di accettazione della tranche

1. Sessione assente, Seller estraneo e permessi insufficienti non leggono né mutano dati.
2. Due Seller possono usare lo stesso nome senza condividere record o prodotti.
3. Il nome fornitore duplicato nello stesso Seller viene rifiutato senza record parziali.
4. CSV con EAN `0123456789012` e costo `12,34` conserva l'EAN e salva `12.34`.
5. XLS/XLSX e XML validi producono le stesse colonne canoniche.
6. File vuoto, malformato, eccessivo o pickle viene rifiutato senza listino incompleto.
7. L'anteprima mostra al massimo 200 righe e il totale persistito.
8. Il listino richiede `ELIMINA`; il fornitore richiede il nome esatto, mostra quanti
   listini contiene e li elimina insieme a tutti i dati collegati.
9. Upgrade, downgrade e nuovo upgrade della migrazione conservano la coerenza prevista.
10. Interfaccia desktop e mobile espone le sottosezioni reali `Fornitori` e `Listini`
    nella macroarea `Catalogo`.

## Criteri candidati, ancora non verificati

La tranche mira alle interazioni `LEGACY-UI-0034`–`0051`, `LEGACY-UI-0063`–`0064` e
`LEGACY-UI-0069`–`0071` limitatamente alle funzioni effettivamente pubblicate, oltre ai
requisiti `MASTER-0044`, `MASTER-0045`, `MASTER-0763`, `MASTER-0764`, `MASTER-0768`,
`MASTER-0776` e `MASTER-0778` quando la relativa prova è completa.

La presenza in questa lista non cambia il ledger. Ogni ID resta `pending` finché
implementazione, test, deploy e collaudo dello staging non ne dimostrano singolarmente
la conformità.

## Sottoblocchi successivi

- B12.1b: URL sicuri, versioni remote e adapter Hurtel, Cecotec, ForceTop,
  ActiveShop, AB Online e Innpro FULL/LIGHT;
- B12.1c: condivisioni private/shared/global entro confini Agency e Platform definiti;
- B12.1d: whitelist contabile persistente e ricalcolo costi sull'unico archivio Ordini,
  inclusa la conservazione dei costi durante le sincronizzazioni successive.
