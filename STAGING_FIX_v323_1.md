# Marketplace Hub SaaS v323.1 — Render Free preDeploy fix

Render non supporta `preDeployCommand` sui Web Service Free.

Correzione:
- rimosso `preDeployCommand` dalla API Free;
- inizializzazione idempotente dello schema eseguita nel `startCommand` prima di FastAPI;
- nessuna modifica a database, worker, frontend, cache o piani.

Nuovo avvio API:

```text
python tools/init_saas_db.py && python tools/run_api.py
```

La produzione `marketplacehub-1` non viene modificata.
