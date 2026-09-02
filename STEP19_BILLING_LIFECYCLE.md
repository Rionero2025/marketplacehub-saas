# Marketplace Hub v319 — Billing & Subscription Lifecycle

La v319 separa definitivamente il ciclo di vita commerciale dal provider di pagamento.
Stripe **non è richiesto** per usare questa release: il Platform Owner può gestire tutto manualmente e, in futuro, Stripe chiamerà lo stesso motore tramite adapter/webhook.

## Stati supportati

- `manual`: accesso gestito manualmente/legacy;
- `trialing`: prova attiva fino a `trial_end`;
- `active`: abbonamento attivo;
- `past_due`: pagamento fallito o rinnovo scaduto, con periodo di grazia;
- `suspended`: accesso sospeso;
- `canceled`: abbonamento terminato.

Durante `past_due` il cliente mantiene l'accesso fino alla scadenza di `grace_period_end`; dopo viene sospeso automaticamente dal refresh del lifecycle.

## Funzioni

- avvio trial (durata configurabile 1-90 giorni);
- attivazione manuale mensile/annuale;
- registrazione pagamento riuscito/fallito;
- grace period;
- sospensione e riattivazione;
- cancellazione immediata o a fine periodo;
- upgrade/downgrade immediato o programmato;
- storico eventi di billing;
- idempotenza tramite `external_event_id`, già pronta per futuri webhook Stripe;
- provider `manual` e placeholder `stripe`, senza dipendenza dalla libreria/API Stripe.

## API Platform Owner

- `GET /api/v1/tenants/{tenant_id}/billing`
- `GET /api/v1/tenants/{tenant_id}/billing/events`
- `POST /api/v1/tenants/{tenant_id}/billing/trial`
- `POST /api/v1/tenants/{tenant_id}/billing/activate`
- `POST /api/v1/tenants/{tenant_id}/billing/payment-success`
- `POST /api/v1/tenants/{tenant_id}/billing/payment-failed`
- `POST /api/v1/tenants/{tenant_id}/billing/suspend`
- `POST /api/v1/tenants/{tenant_id}/billing/resume`
- `POST /api/v1/tenants/{tenant_id}/billing/cancel`
- `POST /api/v1/tenants/{tenant_id}/billing/plan-change`
- `POST /api/v1/tenants/{tenant_id}/billing/refresh`

## Stripe

Nessuna chiave Stripe viene richiesta o memorizzata in v319. Quando sarà disponibile, un adapter Stripe tradurrà eventi come `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated` negli stessi metodi del motore Billing. La business logic non dipende da Stripe.
