"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { WorkspacePage } from "@/components/WorkspacePage";
import { PageHeader } from "@/components/PageHeader";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { api } from "@/lib/api";
import type { MarketplaceAccount } from "@/lib/types";

type InternalSeller = {
  id: number;
  name: string;
  legal_name?: string;
  active?: boolean;
};

type OnboardingStatus = {
  tenant_id: number;
  completed_steps: string[];
  next_step: string;
  sellers: InternalSeller[];
  marketplace_accounts: MarketplaceAccount[];
  billing?: Record<string, unknown> | null;
};

type MarketplaceConnectResult = {
  account_id: number;
  marketplace: string;
  account_name: string;
  validation?: {
    ok?: boolean;
    message?: string;
    shop_name?: string;
    storefronts?: string[];
    locales?: string[];
  };
};

function Body() {
  const { user, seller: workspaceSeller, refresh } = useWorkspace();
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [accounts, setAccounts] = useState<MarketplaceAccount[]>([]);
  const [marketplace, setMarketplace] = useState("kaufland");
  const [clientKey, setClientKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [shopId, setShopId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const internalSeller = useMemo(
    () => workspaceSeller ?? status?.sellers?.[0] ?? null,
    [workspaceSeller, status],
  );

  const load = async () => {
    setLoadingStatus(true);
    try {
      const next = await api<OnboardingStatus>("/onboarding/status");
      setStatus(next);
      setAccounts(Array.isArray(next.marketplace_accounts) ? next.marketplace_accounts : []);
      // Onboarding can repair Seller ownership/scope. Reload the shared context
      // after that operation, rather than leaving the topbar/dashboard stale.
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Impossibile leggere lo stato del workspace.");
    } finally {
      setLoadingStatus(false);
    }
  };

  useEffect(() => {
    void load();
  }, [user?.active_tenant_id]);

  const credentialsReady = marketplace === "kaufland"
    ? Boolean(clientKey.trim() && secretKey.trim())
    : Boolean(apiKey.trim() && shopId.trim());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!credentialsReady || busy) return;

    setBusy(true);
    setMessage("");
    const credentials = marketplace === "kaufland"
      ? { client_key: clientKey.trim(), secret_key: secretKey.trim() }
      : { api_key: apiKey.trim(), shop_id: shopId.trim() };

    try {
      const result = await api<MarketplaceConnectResult>("/onboarding/marketplaces/connect", {
        method: "POST",
        body: JSON.stringify({
          seller_id: internalSeller?.id ?? 0,
          marketplace,
          credentials,
          verify_credentials: true,
        }),
      });

      const storefronts = result.validation?.storefronts?.length
        ? ` · ${result.validation.storefronts.map(value => value.toUpperCase()).join(", ")}`
        : "";
      setMessage(`Connesso: ${result.account_name}${storefronts}`);
      setClientKey("");
      setSecretKey("");
      setApiKey("");
      setShopId("");
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Collegamento non riuscito.");
    } finally {
      setBusy(false);
    }
  };

  return <>
    <PageHeader
      title="Impostazioni"
      description="Workspace interno e collegamenti reali ai marketplace."
    />

    <div className="stats">
      <div className="statCard">
        <span>Tenant</span>
        <strong>{user?.active_tenant_name || "—"}</strong>
        <small>{user?.active_tenant_type || "workspace"}</small>
      </div>
      <div className="statCard">
        <span>Negozio Marketplace Hub</span>
        <strong>{internalSeller?.name || (loadingStatus ? "…" : "Da inizializzare")}</strong>
        <small>contenitore interno del workspace</small>
      </div>
      <div className="statCard">
        <span>Marketplace collegati</span>
        <strong>{accounts.length}</strong>
        <small>{accounts.map(item => item.marketplace).join(" · ") || "nessuno"}</small>
      </div>
      <div className="statCard">
        <span>Configurazione</span>
        <strong>{accounts.length ? "Attiva" : "Da collegare"}</strong>
        <small>{accounts.length ? "API marketplace configurata" : "inserisci le prime credenziali"}</small>
      </div>
    </div>

    <section className="panel">
      <div className="panelTitle">
        <div>
          <h2>Collega il primo marketplace</h2>
          <p>
            Inserisci le credenziali reali. Marketplace Hub verifica la connessione via API e salva le credenziali cifrate solo dopo il controllo.
          </p>
        </div>
      </div>

      <form className="settingsForm" onSubmit={submit}>
        <label>
          Marketplace
          <select value={marketplace} onChange={event => setMarketplace(event.target.value)}>
            <option value="kaufland">Kaufland</option>
            <option value="worten">Worten</option>
          </select>
        </label>

        <label>
          Account marketplace
          <input
            value={accounts.find(item => item.marketplace === marketplace)?.account_name || "Rilevato dopo la connessione API"}
            readOnly
          />
        </label>

        {marketplace === "kaufland" ? <>
          <label>
            Client Key
            <input
              type="password"
              value={clientKey}
              onChange={event => setClientKey(event.target.value)}
              required
              autoComplete="off"
            />
          </label>
          <label>
            Secret Key
            <input
              type="password"
              value={secretKey}
              onChange={event => setSecretKey(event.target.value)}
              required
              autoComplete="off"
            />
          </label>
        </> : <>
          <label>
            API Key
            <input
              type="password"
              value={apiKey}
              onChange={event => setApiKey(event.target.value)}
              required
              autoComplete="off"
            />
          </label>
          <label>
            Shop ID
            <input value={shopId} onChange={event => setShopId(event.target.value)} required />
          </label>
        </>}

        <div className="settingsActions">
          <button className="primaryButton" disabled={busy || !credentialsReady}>
            {busy ? "Verifica connessione…" : "Verifica e collega"}
          </button>
          {message && <span className="formMessage">{message}</span>}
        </div>
      </form>

      <p style={{ marginTop: 12, fontSize: 12, opacity: 0.72 }}>
        Il nome pubblico del negozio viene utilizzato quando il marketplace lo espone tramite API. Se Kaufland non restituisce un nome pubblico, Marketplace Hub usa un alias tecnico basato sui storefront realmente abilitati, senza inventare dati.
      </p>
    </section>

    <section className="panel">
      <div className="panelTitle"><h2>Account collegati</h2></div>
      {accounts.length > 0 && <p>Il collegamento è attivo. <Link href="/orders">Apri Ordini e avvia la sincronizzazione</Link> per caricare i dati nella dashboard.</p>}
      <div className="table">
        <div className="tr th"><span>Marketplace</span><span>Account</span><span>Stato</span></div>
        {accounts.map(account => <div className="tr" key={account.id}>
          <span className="caps">{account.marketplace}</span>
          <span>{account.account_name}</span>
          <span><i className="dot ok"/>Attivo</span>
        </div>)}
      </div>
    </section>
  </>;
}

export default function Page() {
  return <WorkspacePage><Body/></WorkspacePage>;
}
