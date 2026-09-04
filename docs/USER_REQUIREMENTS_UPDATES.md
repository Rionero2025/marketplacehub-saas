# Requisiti confermati — 4 settembre 2026

Il sito pubblico presenta sei piani mensili: Free €0, Bronze €25, Silver €49,
Gold €79, Platinum €150, Enterprise €250. Prezzi modificabili nel tempo.
Pacchetti aggiuntivi futuri per volumi ordini, numero di listini e singole funzioni.
Prezzi e soglie di questi componenti aggiuntivi non sono ancora definiti.

Le tre macroaree dell’app sono:
- Admin della piattaforma: controllo globale del SaaS e dei clienti, indipendente
  dal ruolo Owner di un cliente. Nessuna registrazione pubblica crea un Admin.
- Seller: negozi, marketplace e funzioni del proprio abbonamento.
- Agency: gestione di più seller/clienti assegnati, come nel programma Streamlit.

Si inizia con una prova gratuita Enterprise, con tutte le funzioni operative
abilitate e nessun privilegio Platform Admin. Durata iniziale: i 14 giorni già
configurati, modificabili dalla configurazione server. Il piano selezionato sul
sito è registrato come interesse; la prova effettiva è Enterprise senza addebito.

## Consegna iniziale

Sito pubblico con prezzi da API, registrazione Seller/Agency, prova Enterprise,
prime pagine Admin e Agency e accesso Seller esistente. Admin: elenco tenant,
modifica prezzi persistente e attivazione trial tramite endpoint riservati.
Agency: clienti della specifica agenzia intersecati con gli accessi dell’utente.
I piani vecchi restano disponibili alle sottoscrizioni esistenti ma non nel sito.

Limiti e differenze funzionali fra i nuovi livelli non sono stati inventati:
provvisoriamente le feature operative sono abilitate e i limiti non definiti
sono null. Le API Admin possono configurarli; editor completo, add-on acquistabili,
Checkout Stripe e porting completo delle funzioni restano da completare.
Nessuna sottoscrizione esistente è convertita automaticamente o addebitata.

Questa consegna avanza i blocchi 14, 15, 35 e 36 ma non li chiude. Totale 6/40=15%.
Il design system completo resta nel blocco 12: queste pagine usano gli attuali
componenti del frontend. Non dichiarare completata la parità con Streamlit.
