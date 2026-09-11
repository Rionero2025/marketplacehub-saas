# Misure fisiche dei listini — 11 settembre 2026

Il catalogo espone peso in kg e lunghezza, larghezza e altezza in cm quando
l'unità è esplicita. I campi sono derivati dai dati originali conservati:
non vengono usate le dimensioni in pixel delle immagini. Valori assenti,
non numerici o non positivi rimangono sconosciuti.

Il filtro di esclusione viene eseguito sul server prima del limite di anteprima:
superiore e inferiore sono confronti stretti; tra due soglie include gli estremi.
Le misure sconosciute restano incluse, come in `services/lists.py` del progetto
Streamlit originale, che non viene modificato. Il filtro riguarda la consultazione
del listino e non elimina prodotti né modifica i dati contabili.

## InnPro

Il FULL contiene il peso in grammi e i parametri `Box length`, `Box width`,
`Box height`. Nel feed verificato questi tre parametri non dichiarano un'unità.
Fino a conferma, sono mostrati come valori originali con unità non specificata
e non sono confrontati con soglie in cm. `INNPRO_BOX_UNIT` è quindi `None`.
Il LIGHT non riceve dimensioni inventate se il fornitore non le invia.

## Dati esistenti e rilascio

La migrazione 0014 aggiunge quattro colonne nullable senza riscaricare file o
riscrivere gli artifact. `SqlCatalogsRepository.backfill_measurements(seller_id)`
aggiorna i dati derivati del solo Seller indicato in transazioni da 10 righe.
È ripetibile e comprende le versioni già conservate.

Verifiche locali: 44 test Python (incluse migrazione reversibile, API,
isolamento Seller, backfill e filtro oltre la riga 200); 159 test web;
TypeScript, build Next e Ruff superati.

La verifica di parità complessiva resta 147/2011 = 7,31%: questa aggiunta non
certifica da sola la migrazione completa della gestione listini Streamlit.
