# Riferimenti della ricostruzione

Data di avvio: 7 settembre 2026.

- Repository SaaS precedente, congelato: commit `93cab535e89f36d8149c5298f70463d2c297c7ac`.
- Archivio GitHub: `archive/pre-rebuild-2026-09-07`.
- Repository Streamlit GitHub letto in sola lettura: `Rionero2025/marketplacehub-1`, baseline `4c3cda59387068f3dfb0f2bae45b7d03bf307dca`.
- Installazione locale Streamlit: `C:/Users/Giorgio/Documents/marketplace_hub`, usata esclusivamente come confronto in sola lettura.
- Riferimento live Streamlit: `https://marketplace-hub-wchg.onrender.com/`, consultazione senza operazioni di scrittura.
- Master Spec fornito dal proprietario: requisito aggiuntivo della trasformazione SaaS.

In caso di differenza tra queste sorgenti, la discrepanza deve essere documentata e risolta prima dell’implementazione; non viene scelta automaticamente la variante più semplice.

## Macroaree originali da inventariare

Dashboard; gestione Seller e utenti; fornitori e listini; provider IA; lavorazione listini; creazione prodotti; pubblicazione marketplace; Buy Box; ordini marketplace; prodotti più venduti; ordini Cecotec; ordini Innpro; Packlink; tracciabilità; contabilità; assistenza; cancellazioni; storico; backup/trasferimento; database.

## SaaS

Sito pubblico e piani; accesso Seller; accesso Agency; accesso Platform Admin; isolamento tenant; abbonamenti e funzioni abilitate. Questi elementi avvolgono le funzioni originali e non ne cambiano gli algoritmi.
