const READINESS_BUDGET_MS = 90000;

function cancelled(): Error { const error = new Error("Access cancelled"); error.name = "AbortError"; return error; }
export function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(cancelled()); return; }
    const onAbort = () => { clearTimeout(timer); signal.removeEventListener("abort", onAbort); reject(cancelled()); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Only polls the public readiness endpoint during a submitted login attempt. No credentials or session cookies are sent. */
export async function waitForLoginReadiness({ signal, onWaiting, fetcher = fetch, now = Date.now, pause = abortableDelay }: {
  signal: AbortSignal; onWaiting: () => void; fetcher?: typeof fetch; now?: () => number;
  pause?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
}): Promise<boolean> {
  const deadline = now() + READINESS_BUDGET_MS;
  while (now() < deadline) {
    if (signal.aborted) throw cancelled();
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    const timeout = setTimeout(() => controller.abort(), Math.min(12000, deadline - now()));
    let ready = false;
    try {
      const response = await fetcher("/api/auth/readiness", { method: "GET", cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal });
      const value: unknown = await response.json().catch(() => null);
      ready = response.status === 200 && typeof value === "object" && value !== null && "ready" in value && value.ready === true;
    } catch { /* A cold service may return HTML or time out; only this read is retried. */ }
    finally { clearTimeout(timeout); signal.removeEventListener("abort", onAbort); }
    if (signal.aborted) throw cancelled();
    if (ready && now() < deadline) return true;
    onWaiting();
    const remaining = deadline - now();
    if (remaining <= 0) break;
    await pause(Math.min(2000, remaining), signal);
  }
  return false;
}
