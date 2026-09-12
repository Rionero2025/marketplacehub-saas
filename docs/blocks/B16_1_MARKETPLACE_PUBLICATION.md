# B16.1 / B17.1 — Pubblicazione offerte dal pannello Seller

## Perimetro consegnato

Marketplace (spina) → Pubblica sui marketplace, route `/seller/marketplaces/publish`.
Scelta account collegato e vista associata; Kaufland (sette storefront, Playground o
reale) e Worten Portogallo. Gli altri connettori non dichiarano una capacità di
pubblicazione non implementata.

Anteprima persistente fino a 100 prodotti per invio: ricarico, prezzo minimo,
commissione stimata, costo e guadagno, quantità/costo minimo, esclusione per peso
con misure mancanti mantenute, SKU originale o composto. La vista salvata resta
immutata; i prezzi di pubblicazione sono ricalcolati con le regole esplicite.
Il costo totale comprende la spedizione una sola volta.

Kaufland legge gruppi spedizione e magazzini via API. Preparazione, IVA e cambio
manuale datato CZK/PLN sono configurabili. Worten usa stato tecnico, classe
logistica, origine spedizione e preparazione; CSV Mirakl UTF-8 BOM con colonne
originali, invio multipart `file`, `import_mode=NORMAL` in query.

Conferma PUBBLICA obbligatoria. Nessuna pubblicazione automatica durante la
preparazione o la selezione di un account. Gli invii esplicitamente confermati
continuano sul worker con stato per riga e percentuale delle righe elaborate.
Accettato dall'API e importazione ricevuta NON significano offerta già visibile.

## Persistenza e controlli

Migrazione 0016 aggiunge solo job e ricevute; upgrade/downgrade testati preservando
i dati preesistenti. Snapshot del payload e delle regole, revisione vista,
impronta privata delle credenziali, scadenza anteprima di due ore.
Identità Seller/organizzazione e permesso CATALOG vengono verificati nelle API e
nuovamente prima dell'invio del worker. Credenziali decifrate solo sul server.
Una sola esecuzione attiva per account, acquisizione atomica, token di esecuzione
per impedire che un vecchio worker riprenda un tentativo successivo.

POST esterni senza retry automatico. Timeout/esito ambiguo → da verificare;
ripresa solo delle righe ancora pending. Una risposta tardiva non ripristina
le righe già marcate unknown. Rifiuti API distinti dalle richieste ambigue.
La ripresa interrompe i job running senza avanzamento per tre minuti; i job
queued possono attendere trenta minuti. Nessun segreto nei DTO/storico/errori.

## Verifiche

- 18 test Python: nuova pubblicazione, migrazione e integrazione coda worker.
- 8 regressioni della lavorazione listini passate.
- 163 test web esistenti e 6 test nuovi proxy/DTO/navigazione passati.
- TypeScript e build Next riusciti, nuova route compresa.
- 42 confronti diretti con `services/kaufland_offer.py` originale, letto soltanto:
  prezzi, minimo, commissione, guadagno, SKU e conversione in centesimi identici.
  SHA-256 sorgente: `60f781bd02c9a1c90959319e7c25d81440a1e452cc073951ab413c00ce820ec9`.
- Test autenticato staging su RioneroShop: account Kaufland principale, vista
  Innpro di 3.363 righe; anteprima delle prime tre, ricarico 35%, minimo 10%,
  commissione 15%, SKU composto. Prima riga EAN 6930460000040:
  costo salvato 52,93 €, vendita 71,46 €, minimo 58,22 €, commissione 10,72 €,
  guadagno 7,81 €, SKU `InnPro_6930460000040_52.93_58.22`, quantità 258.
  La vista esistente non contiene nomi per queste righe: visualizzati mancanti.
  Nessuna modifica alla vista o ai listini del fornitore.
- Nessun POST di offerte reali eseguito nel collaudo. Le ricevute esterne e
  i timeout sono verificati con trasporto simulato; collaudo commerciale reale
  da effettuare con una selezione esplicita dell'utente.

## Rilascio e copertura

Implementazione `c8362ad`, correzione selezione configurazione `2937d77`,
riapertura delle regole e controllo spedizione `4f2c07a`.
I commit iniziali sono stati verificati Live su sito e worker, con pagina
autenticata e API catalogo/pubblicazione funzionanti. Kaufland ha restituito
due gruppi spedizione (Europa:de 153679, free 105166) e un magazzino (RIONERO
58219); selezione verificata nei menu. Una seconda anteprima di tre righe è
stata preparata con Europa:de e RIONERO, senza confermare PUBBLICA.
Commit finale `4f2c07a456090d1829c839de7148a41d441b318c` verificato Live
su tutti e tre i servizi Render: web, API e worker. Riapertura autenticata
della seconda anteprima verificata: account, vista, ambiente reale, tre
prodotti, gruppo 153679 e magazzino 58219 ripristinati; conferma vuota e
pulsante Pubblica selezionati disabilitato. Health web e readiness API HTTP 200.

Promossi solo LEGACY-UI-0268, 0269, 0270, 0272, collaudati nel browser:
account Kaufland, ambiente, vista e SKU composto. Nessun criterio di invio reale
o di completamento integrale dei moduli B16/B17 viene promosso.
Copertura finale: 163/2.011 = 8,11%.

## Ancora da trasferire dall'originale

Salvataggio/riuso autonomo delle regole commerciali, cambio BCE automatico,
esecuzione simultanea su più Paesi, download CSV Worten
e memoria trasversale agli invii separati. Le regole speciali dei fornitori
restano nel relativo blocco cataloghi; non vengono sostituite silenziosamente.
Il monitoraggio dell'esito definitivo Mirakl e della visibilità delle offerte
non è incluso nella ricevuta di accettazione di questo primo blocco.

Editor dell’anteprima completato successivamente nel blocco B16.2: vedere `B16_2_PUBLICATION_EDITOR.md`.
