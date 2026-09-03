# Marketplace Hub SaaS v323.3 — Frontend TypeScript fix

Il build Next.js arrivava correttamente al type-check e falliva in
`frontend/src/components/AppShell.tsx` sulla `flatMap()` di `navGroups`.

Causa:
`navGroups` era dichiarato `as const`, quindi TypeScript inferiva tre tuple
readonly eterogenee con tipi letterali incompatibili tra loro durante `flatMap`.

Correzione:
- introdotti i tipi comuni `NavItem` e `NavGroup`;
- `navGroups` è ora `readonly NavGroup[]`;
- rimossa la cast non più necessaria su `item.icon`.

Nessuna modifica a API, Worker, PostgreSQL, Redis, secret o Render.
