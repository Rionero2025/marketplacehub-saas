# Marketplace Hub v312 — Saved Views + Object Storage Ready

## Obiettivo
La v312 rimuove la dipendenza strutturale delle viste salvate dal solo filesystem locale.
Le viste continuano ad avere una copia locale di compatibilità/cache, ma il riferimento durevole è ora un object key persistente.

## Nuovo layer
- `services/object_storage.py`: backend locale e backend S3-compatible.
- `services/saved_view_storage.py`: salvataggio, lettura, materializzazione locale e migrazione delle viste.
- colonne additive su `saved_views`: `snapshot_storage_key`, `snapshot_storage_backend`, `snapshot_sha256`, `snapshot_size_bytes`.

## Comportamento
1. Quando una vista viene salvata, il DataFrame viene serializzato una sola volta.
2. Lo snapshot viene scritto nell'object storage configurato.
3. `snapshot_path` resta una cache/copertura di compatibilità per il codice legacy.
4. Se il container riparte e il file locale non esiste più, `resolve_saved_view_path()` lo ricrea automaticamente scaricandolo dallo storage.
5. L'hash SHA-256 viene verificato prima di usare una copia scaricata.

## Backend supportati
### Locale
Default per sviluppo e test:
`MARKETPLACE_HUB_STORAGE_BACKEND=local`

### S3-compatible
Per AWS S3, Cloudflare R2, MinIO o provider con API S3:
- `MARKETPLACE_HUB_STORAGE_BACKEND=s3`
- `MARKETPLACE_HUB_STORAGE_BUCKET`
- `MARKETPLACE_HUB_STORAGE_ENDPOINT_URL` (facoltativo per AWS, richiesto tipicamente per R2/MinIO)
- `MARKETPLACE_HUB_STORAGE_REGION`
- `MARKETPLACE_HUB_STORAGE_ACCESS_KEY`
- `MARKETPLACE_HUB_STORAGE_SECRET_KEY`

Le credenziali non devono essere committate nel repository.

## Migrazione viste esistenti
In **Lavora sui listini** compare `Migra viste legacy` quando esistono snapshot senza object key.
La migrazione non modifica la logica commerciale né i dati dei prodotti: crea soltanto una copia persistente nello storage configurato.

## Compatibilità
I principali consumatori delle viste (Pubblicazione Kaufland/Worten, Buy Box, Contabilità, Cecotec, Packlink e Creazione Prodotti) possono ricostruire la cache locale dallo storage.

## Passo successivo
La stessa astrazione verrà estesa ai file sorgente dei listini, allegati, Excel/PDF e artifact di pubblicazione, così il web service non avrà bisogno di persistent disk locale.
