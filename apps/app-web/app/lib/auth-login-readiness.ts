const READINESS_BUDGET_MS = 60000;
const READINESS_REQUEST_MS = 8000;
const READINESS_RETRY_INTERVAL_MS = 1500;
const READINESS_RECOVERY_INTERVAL_MS = 5000;

function cancelled(): Error { const error = new Error("Access cancelled"); error.name = "AbortError"; return error; }
export function monotonicNow(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
}
export function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(cancelled()); return; }
    const onAbort = () => { clearTimeout(timer); signal.removeEventListener("abort", onAbort); reject(cancelled()); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Performs one anonymous, read-only readiness probe. */
export async function probeLoginReadiness({ signal, fetcher = fetch, timeoutMs = READINESS_REQUEST_MS }: {
  signal: AbortSignal; fetcher?: typeof fetch; timeoutMs?: number;
}): Promise<boolean> {
  if (signal.aborted) throw cancelled();
  const controller = new AbortController();
  let rejectCancellation: ((reason: Error) => void) | undefined;
  const cancellation = new Promise<boolean>((_resolve, reject) => { rejectCancellation = reject; });
  const onAbort = () => { controller.abort(); rejectCancellation?.(cancelled()); };
  signal.addEventListener("abort", onAbort, { once: true });
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const timeoutResult = new Promise<boolean>((resolve) => {
    timeout = setTimeout(() => { controller.abort(); resolve(false); }, Math.max(0, timeoutMs));
  });
  const request = (async () => {
    try {
      const response = await fetcher("/api/auth/readiness", { method: "GET", cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal });
      const value: unknown = await response.json().catch(() => null);
      if (signal.aborted) throw cancelled();
      return response.status === 200 && typeof value === "object" && value !== null && "ready" in value && value.ready === true;
    } catch {
      if (signal.aborted) throw cancelled();
      return false;
    }
  })();
  try {
    return await Promise.race([request, timeoutResult, cancellation]);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", onAbort);
  }
}

/** Polls readiness during a submitted login attempt. No credentials or session cookies are sent. */
export async function waitForLoginReadiness({ signal, onWaiting, fetcher = fetch, now = monotonicNow, pause = abortableDelay,
  budgetMs = READINESS_BUDGET_MS, requestTimeoutMs = READINESS_REQUEST_MS, retryIntervalMs = READINESS_RETRY_INTERVAL_MS }: {
  signal: AbortSignal; onWaiting: () => void; fetcher?: typeof fetch; now?: () => number;
  pause?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  budgetMs?: number; requestTimeoutMs?: number; retryIntervalMs?: number;
}): Promise<boolean> {
  const deadline = now() + Math.max(0, budgetMs);
  while (now() < deadline) {
    if (signal.aborted) throw cancelled();
    // A request opened while the service is asleep can remain stale even after a
    // fresh probe succeeds. Bound each attempt so polling can observe recovery.
    const ready = await probeLoginReadiness({ signal, fetcher, timeoutMs: Math.min(Math.max(0, requestTimeoutMs), deadline - now()) });
    if (signal.aborted) throw cancelled();
    if (ready) return true;
    onWaiting();
    const remaining = deadline - now();
    if (remaining <= 0) break;
    await pause(Math.min(Math.max(0, retryIntervalMs), remaining), signal);
  }
  return false;
}

/** Keeps checking anonymously after the visible login wait expires. Resolves only when ready. */
export async function recoverLoginReadiness({ signal, fetcher = fetch, pause = abortableDelay }: {
  signal: AbortSignal; fetcher?: typeof fetch;
  pause?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
}): Promise<void> {
  while (true) {
    if (signal.aborted) throw cancelled();
    if (await probeLoginReadiness({ signal, fetcher })) return;
    await pause(READINESS_RECOVERY_INTERVAL_MS, signal);
  }
}
