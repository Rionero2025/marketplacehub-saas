# Integrazioni

| Dominio | Integrazione rilevata | Comportamenti da preservare |
|---|---|---|
| Marketplace | Kaufland Seller API v2 e playground | account, ordini, offerte, inventario, categorie, Buy Box, prezzi, cancellazioni, ticket e tracking |
| Marketplace | Worten tramite API Mirakl | account, offerte/prodotti, ordini, Buy Box, cancellazioni, supporto e tracking |
| Fornitore | Cecotec | listino/stock, match EAN e SKU, paesi, generazione ordine nel template ufficiale |
| Fornitore | Innpro | feed, riconoscimento SKU composito, match EAN, costi, export e storico |
| Fornitore | AB Online | gateway XML e verifica IP pubblico necessaria al canale |
| Fornitore | ActiveShop | feed prodotti e stock separato |
| Spedizioni | Packlink PRO API | registrazione integrazione, mittente, colli, tariffe, scelta migliore, spedizione e persistenza |
| Spedizioni | Packlink CSV ufficiale | fallback e compatibilità del tracciato |
| Valute | BCE | conversioni EUR/PLN/CZK e altri tassi disponibili |
| IA | OpenAI, Anthropic, Gemini, Mistral, xAI, DeepSeek, Groq, OpenRouter, Azure/Ollama compatibili | profili cifrati, fallback, JSON strutturato e contatori utilizzo |
| Dati | Excel, Google Sheets e URL HTTPS | listini, confronto contabile, tracking, import/export e controlli URL |
| Documenti | PDF/OCR | documenti fornitore, ricevute e report contabili |

Roadmap indicata dal Master Spec: About You, OBI, KuantoKusta e altri canali Mirakl tramite adapter comune. Questi canali non sono considerati implementati finché non esiste un flusso reale verificato.

Per ogni integrazione la nuova implementazione deve conservare autenticazione, paginazione, limiti, retry, mapping, cache, errori e idempotenza osservati nel codice originale. I test con mock non sostituiscono la verifica delle credenziali e delle risposte reali in staging.
