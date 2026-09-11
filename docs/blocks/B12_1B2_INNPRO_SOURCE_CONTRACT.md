# B12.1b2 — Contratto sorgente InnPro FULL/LIGHT

Data del contratto: 11 settembre 2026. Perimetro: pannello Seller Enterprise.
Il repository Streamlit originale, congelato al commit `7c8e5f9`, resta in sola
lettura.

Stato del documento: **CONTRATTO SORGENTE; IMPLEMENTAZIONE VERIFICATA
LOCALMENTE, COLLAUDO STAGING IN ATTESA**.

## Risultato osservabile richiesto

Il Seller deve poter associare allo stesso fornitore due feed InnPro distinti:

- **FULL**, sorgente autorevole dei contenuti prodotto;
- **LIGHT**, sorgente autorevole di costo di acquisto e disponibilità.

La scelta `InnPro IOF` e il ruolo `FULL` oppure `LIGHT` devono essere espliciti
sia per il caricamento da file sia per il collegamento da URL. I due feed possono
coesistere e produrre versioni indipendenti senza essere confusi con un listino
generico.

## Contratto funzionale

| Area | InnPro FULL | InnPro LIGHT |
| --- | --- | --- |
| Scopo | anagrafica e contenuti prodotto | costo all'ingrosso e stock |
| Chiave di collegamento | EAN testuale normalizzato | EAN testuale normalizzato |
| Campi principali | nome, EAN, SKU, varianti, produttore/marca, categoria, descrizioni multilingua, immagini, documenti, parametri, pesi e prezzo consigliato | EAN, prezzo di acquisto reale e quantità disponibile |
| Uso contabile | non può fornire il costo | unica sorgente InnPro ammessa per il costo |
| Attivazione | solo dopo parsing completo della nuova versione | solo dopo parsing completo della nuova versione |

Il ruolo del feed è parte immutabile dell'identità del listino. Sono valide
soltanto le coppie `generic + standard`, `innpro + full` e `innpro + light`.
Cambiare ruolo a un listino già creato richiede un nuovo listino, così lo storico
non può assumere un significato diverso dopo l'importazione.

## Regola contabile InnPro

La risoluzione del costo segue questo ordine obbligatorio:

1. individua il Seller, l'organizzazione e l'ambiente dell'ordine;
2. considera soltanto listini InnPro `LIGHT` attivi e accessibili nello stesso
   perimetro;
3. cerca una corrispondenza **esatta per EAN**;
4. applica il costo unitario LIGHT alla quantità dell'ordine e ricalcola i valori
   economici previsti dall'archivio Ordini;
5. se la sorgente non è disponibile, la query fallisce o l'EAN non coincide, non
   usa FULL, SKU, SKU composto o dati di un altro Seller come ripiego.

Questa è una regola *fail closed*: l'assenza di una corrispondenza sicura produce
un costo non disponibile, anziché un costo plausibile ma non dimostrato. Il feed
FULL conserva le informazioni utili a mostrare e arricchire il prodotto, ma non
partecipa mai al calcolo del costo.

## Parsing e normalizzazione

L'adapter legge XML IOF con namespace variabili mediante parsing incrementale.
Non costruisce l'intero documento in memoria e libera gli elementi già elaborati.
EAN e SKU restano stringhe, così gli eventuali zeri iniziali vengono conservati.
Il ruolo dichiarato dall'utente viene confrontato con quello rilevato: una
discordanza interrompe l'importazione prima dell'attivazione.

Il parser deve mantenere, quando presenti, i campi FULL necessari a ricostruire
la scheda prodotto originale, comprese varianti, testi in più lingue, media,
documenti tecnici, parametri e misure. LIGHT mantiene il prezzo all'ingrosso e
la disponibilità senza promuovere campi mancanti a valori inventati.

## Artefatti grandi e persistenza durevole

I feed remoti InnPro possono raggiungere 200 MiB. Il worker scarica la risposta su
un file temporaneo con limite e deadline, esegue il parsing dal disco e cancella
sempre il file al termine. Il limite generico da 20 MiB resta invariato per le
sorgenti che non usano questo percorso dedicato.

L'artefatto originale viene salvato in PostgreSQL con codifica:

- `identity` fino a 20 MiB;
- `gzip` deterministico oltre 20 MiB;
- massimo 200 MiB prima della compressione;
- massimo 64 MiB dopo la compressione.

SHA-256 e dimensione registrati si riferiscono sempre ai byte originali. Ogni
lettura verifica decompressione, dimensione e hash; dati compressi corrotti non
vengono esposti come versione valida. La versione precedente resta attiva se
download, parsing, compressione o persistenza falliscono.

## Prove sui file originali

| Feed | Dimensione originale | SHA-256 | Risultato parser |
| --- | ---: | --- | ---: |
| InnPro FULL | 111.012.011 byte | `b7fcb8a17f89f7e733e03a0b21569b87660dcd1ace1407ecf001cbaa4976f17e` | 6.880 prodotti/righe |
| InnPro LIGHT | 1.621.952 byte | `9cf2a50e8be89eff8f501b3e08157f8788bdaba951aecc99067283309c0f4b57` | 5.000 prodotti/righe |

La prova end-to-end locale ha attraversato download su disco, parsing, creazione
della versione e dell'artefatto, query del catalogo e risoluzione del costo LIGHT
per EAN esatto. I file originali sono stati soltanto letti; nessuna copia di prova
è stata mantenuta nel repository.

## Isolamento e autorizzazione

Fornitore, listino, versione, artefatto, prodotto e costo sono sempre vincolati a
`organization_id + seller_id`. Un ID valido di un altro Seller non deve produrre
né dati né differenze osservabili utili a dedurne l'esistenza. Tutte le mutazioni
richiedono il permesso Catalogo in scrittura; le letture richiedono il relativo
permesso sul negozio attivo.

La ricerca contabile viene eseguita lato server sul database autorizzato. Il
browser non sceglie quale feed possa fornire il costo e non invia il costo da
salvare nell'ordine.

## Casi di accettazione

1. FULL e LIGHT possono coesistere sullo stesso fornitore e conservano ruoli
   distinti nelle versioni successive.
2. Un XML InnPro valido viene riconosciuto anche con namespace differenti.
3. Un feed dichiarato FULL ma rilevato LIGHT, o viceversa, viene rifiutato senza
   cambiare la versione attiva.
4. Il FULL reale da 111.012.011 byte produce esattamente 6.880 prodotti/righe.
5. Il LIGHT reale da 1.621.952 byte produce esattamente 5.000 prodotti/righe.
6. Il FULL conserva i contenuti prodotto disponibili e non fornisce mai il costo
   contabile.
7. Il costo deriva soltanto da LIGHT attivo con EAN esatto e rispetta quantità,
   utile e percentuali previste dall'archivio Ordini.
8. EAN assente o differente, query fallita o LIGHT non attivo lasciano il costo
   indisponibile; nessun fallback usa SKU o FULL.
9. Un Seller non può leggere feed, prodotti o costi di un altro Seller.
10. Un artefatto grande è compresso, verificato e ricostruibile con gli stessi
    byte, dimensione e SHA-256 dell'originale.
11. File temporanei e dati parziali vengono rimossi dopo successo o errore.
12. Upgrade e downgrade delle migrazioni rispettano i limiti di rappresentabilità
    e non degradano silenziosamente artefatti compressi.

## Criteri candidati, ancora non verificati

| ID | Funzione candidata | Stato ledger |
| --- | --- | --- |
| `MASTER-0228` | gestione distinta dei feed InnPro FULL e LIGHT | `pending` |
| `MASTER-0229` | scelta del listino appropriato in base alla funzione | `pending` |
| `MASTER-0230` | prezzi all'ingrosso corretti dal feed LIGHT | `pending` |

L'implementazione e le prove locali non modificano il ledger. I tre criteri
restano `pending` fino al collaudo autenticato nello staging. Il totale certificato
rimane **147/2.011 = 7,31%**; se soltanto questi tre criteri superano il collaudo,
diventa **150/2.011 = 7,46%**.
