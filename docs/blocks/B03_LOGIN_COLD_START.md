# B03 — Accesso durante il risveglio del servizio

Il 7 settembre 2026 l'utente ha segnalato `Impossibile accedere. Riprova.`. La prima GET
di verifica alle API staging ha impiegato 32,406 secondi; subito dopo la readiness ha
risposto 200 in 157 ms, con database e Redis operativi. I log Render confermano un nuovo
avvio del backend. Una richiesta login incompleta e senza credenziali attraverso il BFF
ha poi restituito 422 in 328 ms: il collegamento web/API funziona a servizio avviato.

L'avvio a freddo è compatibile con il problema osservato; non è stata acquisita la risposta
HTTP della specifica richiesta dell'utente. Non si attribuisce l'errore a una password errata.
[Render documenta il risveglio delle istanze Free dopo inattività](https://render.com/docs/free).

Il form attende la readiness con sole richieste GET senza credenziali, per un massimo di
90 secondi durante il tentativo di accesso. Mostra l'avvio del servizio e poi invia un solo
POST di autenticazione. Non ripete automaticamente la password, non mantiene il servizio
attivo in background e non cambia il piano Render. Timeout, credenziali rifiutate, limite
tentativi e risposte non valide ricevono messaggi distinti. Il BFF controlla origine,
input e sessione restituita; non inoltra errori HTML o dettagli interni al browser.

Verifiche: typecheck, build produzione e 68 test frontend, inclusi 16 casi login/readiness.
I casi includono risveglio dopo risposta HTML/503, scadenza del tempo, annullamento, doppio
clic, invio singolo, validazione della sessione e mancata esposizione di errori/credenziali.

La correzione non aggiunge un modulo operativo né incrementa il ledger: **87/2.011 = 4,33%**.
Il blocco Ordini B20/B21 rimane in lavorazione separata.
