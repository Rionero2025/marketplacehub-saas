# Marketplace Hub SaaS v323.2 — Render Python import-path fix

Il build Render riusciva, ma il deploy API falliva con:

`ModuleNotFoundError: No module named 'api'`

Causa:
gli script venivano eseguiti direttamente da `tools/`, quindi Python usava
`tools/` come path principale e non vedeva i package nella root del repository.

Correzione:
- API:
  `python -m tools.init_saas_db && python -m tools.run_api`
- Worker:
  `python -m tools.run_worker --poll 0.5`

Rimane applicata anche la correzione v323.1:
nessun `preDeployCommand` sul Web Service Free.

Non vengono modificati database, piani, secret o produzione `marketplacehub-1`.
