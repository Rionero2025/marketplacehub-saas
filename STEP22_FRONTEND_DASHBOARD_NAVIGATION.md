# Marketplace Hub v322 — Frontend Dashboard & Navigation Pro

La v322 consolida la nuova UI Next.js introdotta in v321 senza modificare la logica operativa del Core.

## Obiettivi
- navigazione SaaS senza refresh completo;
- sidebar organizzata per aree e filtrata dai permessi reali;
- selettori Tenant/Seller sempre disponibili nel workspace;
- stato dei worker visibile dalla topbar;
- Dashboard operativa costruita sui dati API già persistenti;
- onboarding visuale del workspace;
- layout responsive per desktop/tablet/mobile.

## Dashboard
La Dashboard non scarica archivi completi. Per ogni account richiede solo il totale server-side (`limit=1`), lo stato/cache contabile, i cataloghi disponibili e gli ultimi job. Il browser riceve quindi soltanto dati riepilogativi.

## Worker pulse
La topbar interroga gli ultimi job del Seller ogni 6 secondi e mostra soltanto stato/progresso. Nessuna operazione lunga viene eseguita dal frontend.

## Compatibilità
- Core Python v320+: invariato;
- FastAPI v314+: invariato;
- Multi-tenant/RLS/Entitlements/Billing/Onboarding: invariati;
- Streamlit legacy: invariato;
- nessuna nuova dipendenza JavaScript esterna.
