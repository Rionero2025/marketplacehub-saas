# v305 — Worker Engine persistente

## Obiettivo
Le operazioni pesanti non devono più vivere nel ciclo di rendering Streamlit.
La v305 introduce una coda job persistente nel database e un worker riutilizzabile.

## Cosa cambia
- tabella `background_jobs` con stato, percentuale, messaggio, risultato ed errore;
- claim atomico dei job; PostgreSQL usa `FOR UPDATE SKIP LOCKED` per più worker concorrenti;
- daemon worker locale di transizione: la UI torna disponibile subito;
- CLI `python tools/run_worker.py` pronta per un servizio Worker separato su Render;
- sincronizzazione ordini Kaufland avviata in background;
- controllo rapido Buy Box Kaufland avviato in background;
- nessuna password/API key viene salvata nel payload job: il worker legge le credenziali cifrate dal DB.

## Architettura
UI -> INSERT background_jobs -> worker -> Core -> servizi marketplace -> PostgreSQL -> UI legge stato.

La coda è già persistente. Quando creeremo un vero Render Background Worker, la UI non dovrà essere riscritta.
