# B20.2 / B21.2 — Filtri, selezione, riepilogo ed esportazione

Fonti in sola lettura: `C:/Users/Giorgio/Documents/marketplace_hub/pages/3_Ordini_Kaufland.py`, righe 596–876, 1040–1195 e 1430–1565; `services/order_selection.py`. Questo blocco non introduce pubblicazioni marketplace o recupero da listini non ancora trasferiti.

## Comportamento da conservare

- Stato, nazione e valuta: tutte le opzioni selezionate inizialmente; selezione esplicitamente vuota = nessuna riga. Corrieri vuoti = nessun filtro. Tracking e commissione: tutti/presenti/mancanti; commissione zero è presente. Limiti del venduto EUR inclusivi. Date secondo il giorno UTC, ricerca letterale inclusi tracking/corriere.
- La selezione originale è memorizzata **per firma del filtro**, account, Seller e ambiente. Al primo accesso a una nuova firma tutte le righe filtrate sono selezionate; tornando alla stessa firma si ripristina la scelta salvata, intersecata con le righe ancora visibili. Una nuova firma non eredita la selezione del filtro precedente. La pagina non fa parte della firma.
- Il SaaS conserva la selezione in SQL per sessione autenticata e perimetro autorizzato. Al browser arrivano soltanto gli ID selezionati nella pagina corrente e i conteggi globali; inizializzazione/pruning usano query SQL senza inviare l'intero archivio.
- Riepilogo del solo insieme selezionato: cancellati visibili con contributo monetario nullo; vendita/commissione/netto vengono sommati soltanto quando tutti e tre sono disponibili. Costo/utile vengono sommati insieme soltanto quando entrambi sono disponibili e la riga non è cancellata. Percentuale utile = utile totale / costo totale; mancanze e valute incomplete sono esplicite. Non vengono inventati pagamenti disponibili/in attesa o quote del gestore.
- CSV separati per selezionati e intero filtro, UTF-8 con BOM, stessi importi/dettagli attualmente esposti. Nessun limite derivato dalla pagina, nessun payload privato, indirizzo o credenziale. Celle testuali rese innocue per i fogli di calcolo; numeri negativi restano numeri.
- Come nello `st.stop` originale, zero righe selezionate impedisce anche il CSV dell'intero filtro. I limiti date/importi disponibili sono restituiti nell'elenco per inizializzare i controlli al minimo/massimo dell'archivio; il filtro API esplicitamente assente resta distinto da quello inizializzato.
- Selezione ed export richiedono LOGISTICS in lettura e controllano sessione, Seller, account, ambiente e firma del filtro. L'esportazione è progressiva e ricontrolla l'autorizzazione per ogni blocco.

## Persistenza e rilascio

Migrazione `20260907_0007`: proiezioni tipizzate di tracking, commissione, valuta, corriere e importi, più selezioni e appartenenze con FK alla sessione. Il backfill elabora 100 righe per volta senza cambiare JSON originali o altri dati. La cancellazione della sessione elimina le sole selezioni associate; durante gli accessi vengono inoltre rimosse le selezioni UI delle precedenti sessioni scadute/revocate dello stesso utente, conservando le sessioni di audit.

Un unico snapshot comprende elenco, conteggio e riepilogo; PostgreSQL usa `REPEATABLE READ`, lock della selezione e retry dei conflitti di serializzazione/deadlock. L'inizializzazione `INSERT SELECT` avviene solo per una nuova firma, quindi aggiornamenti e letture concorrenti non annullano un comando “Deseleziona tutto”. Le proiezioni hanno una versione temporale: eventuali scritture di un vecchio worker durante il deploy vengono ricostruite nel perimetro richiesto prima dei filtri.

## Limiti dichiarati

Scadenziario pagamenti, ticket, modifica/import tracking e listini pubblicati restano fuori dal blocco. La tabella unificata Worten usa la normalizzazione contabile già trasferita; non viene dichiarata la copia di una pagina Ordini Worten inesistente nell'originale.

L'opzione “Valuta non disponibile” rende filtrabili le righe senza valuta; l'originale ometteva il valore vuoto dalle opzioni. È una differenza di usabilità esplicita. Il CSV contiene i 37 campi correnti di tabella/dettaglio: non equivale alle 36 colonne dell'export Streamlit, che includono pagamenti e ticket ancora mancanti.
