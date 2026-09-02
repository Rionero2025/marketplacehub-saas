# Marketplace Hub v307 — Shared Cache / Redis-ready

## Obiettivo
Ridurre le query PostgreSQL ripetitive prodotte da rerun Streamlit, sessioni browser multiple e futuri worker separati, senza cambiare la business logic.

## Cosa cambia
- Nuovo `services/shared_cache.py`: usa Redis quando `MARKETPLACE_HUB_REDIS_URL`/`REDIS_URL` è configurato; altrimenti usa automaticamente la cache TTL locale già introdotta nel Core.
- Cache condivisibile per sessione utente, elenco Seller autorizzati e listini accessibili.
- `services/cache_invalidation.py` invalida automaticamente i namespace interessati dopo INSERT/UPDATE/DELETE sulle tabelle configurate.
- `services.db.execute()` / `execute_many()` notificano il cache layer dopo una scrittura, senza obbligare le pagine Streamlit a gestire la cache.
- L'autorizzazione continua a essere verificata: il browser evita query a ogni rerun e, con Redis, processi diversi condividono lo stesso risultato breve.

## Sicurezza
La password non viene mai salvata nel browser o in Redis. La cache utente contiene il record applicativo già esistente (incluso l'hash PBKDF2, non la password) solo per TTL breve; le modifiche utente invalidano il namespace.

## Configurazione futura SaaS
```env
MARKETPLACE_HUB_REDIS_URL=redis://...
MARKETPLACE_HUB_CACHE_NAMESPACE=marketplacehub:v307
MARKETPLACE_HUB_CACHE_TTL_SECONDS=20
```

Redis non è obbligatorio per avviare il pacchetto: senza URL il programma mantiene il fallback locale.

## Verifica
```bash
python tools/cache_probe.py
pytest -q tests_core
```
