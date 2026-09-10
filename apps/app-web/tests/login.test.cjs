const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

function load(relativePath, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, setTimeout, clearTimeout, URL, ...context });
  return exports;
}
const contract = load("../app/lib/auth-login-contract.ts");
const readiness = load("../app/lib/auth-login-readiness.ts");
const credentials = { login: " User ", password: " password-with-spaces ", realm: "seller" };
const session = { user_id: "a1000000-0000-4000-8000-000000000001", login: "User", display_name: "Demo", realm: "seller", expires_at: "2027-01-01T00:00:00Z" };
class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
function route(file, fetchImpl, extra = {}) {
  return load(file, { fetch: fetchImpl, require(name) {
    if (name === "next/server") return { NextResponse };
    if (name.endsWith("/api-url")) return { apiUrl: "http://api.test" };
    if (name.endsWith("/auth-login-contract")) return contract;
    throw new Error(name);
  }, ...extra });
}
const request = (value = credentials, headers = {}) => ({ url: "https://app.test/api/auth/login", headers: new Headers(headers), json: async () => value });

test("login validation preserves password bytes, validates realm and limits, and discards unknown input", () => {
  const input = contract.readLoginInput({ ...credentials, debug: "secret" });
  assert.equal(input.login, "User"); assert.equal(input.password, credentials.password); assert.equal(input.debug, undefined);
  for (const value of [null, [], { ...credentials, realm: "admin" }, { ...credentials, password: "" }, { ...credentials, password: "x".repeat(1025) }, { ...credentials, password: {} }, { ...credentials, login: " " }, { ...credentials, login: "x".repeat(255) }]) assert.equal(contract.readLoginInput(value), null);
});

test("readiness route returns only safe readiness and sends neither cookies nor login credentials", async () => {
  let options;
  const { GET } = route("../app/api/auth/readiness/route.ts", async (url, init) => {
    assert.equal(url, "http://api.test/health/ready"); options = init;
    return Response.json({ status: "ok", checks: { database: "secret infrastructure" }, version: "internal" });
  });
  const response = await GET(); assert.equal(response.status, 200); assert.deepEqual(await response.json(), { ready: true });
  assert.equal(response.headers.get("cache-control"), "no-store"); assert.equal(options.method, "GET"); assert.equal(options.body, undefined); assert.equal(options.headers.cookie, undefined); assert.ok(options.signal instanceof AbortSignal);
});

test("readiness BFF keeps the upstream wake-up request open for 60 seconds", async () => {
  let timeout; const signal = new AbortController().signal;
  const { GET } = route("../app/api/auth/readiness/route.ts", async (_url, options) => {
    assert.equal(options.signal, signal); return Response.json({ status: "ok" });
  }, { AbortSignal: { timeout(milliseconds) { timeout = milliseconds; return signal; } } });
  assert.equal((await GET()).status, 200); assert.equal(timeout, 60000);
});

test("readiness HTML, degraded service and network errors become safe not-ready responses", async () => {
  for (const result of [() => new Response("<html>secret infrastructure</html>", { status: 502 }), () => Response.json({ status: "degraded", password: "private" }), () => Response.json({ status: "ok" }, { status: 201 }), () => { throw new Error("private host"); }]) {
    const { GET } = route("../app/api/auth/readiness/route.ts", async () => result());
    const response = await GET(); assert.equal(response.status, 503); assert.deepEqual(await response.json(), { ready: false });
  }
});

test("login BFF rejects invalid body and forged origin before forwarding any password", async () => {
  let calls = 0; const { POST } = route("../app/api/auth/login/route.ts", async () => { calls++; throw new Error("must not call"); });
  for (const value of [null, { ...credentials, password: {} }, { ...credentials, realm: "unknown" }]) {
    const response = await POST(request(value)); assert.equal(response.status, 422); assert.equal((await response.text()).includes("password-with-spaces"), false);
  }
  assert.equal((await POST({ ...request(), json: async () => { throw new Error("raw-password"); } })).status, 422);
  assert.equal((await POST(request(credentials, { host: "app.test", origin: "https://evil.test", "x-forwarded-host": "evil.test" }))).status, 403);
  assert.equal(calls, 0);
});

test("login BFF posts exactly once, keeps password whitespace and forwards cookie only for a matching valid session", async () => {
  const calls = [];
  const { POST } = route("../app/api/auth/login/route.ts", async (url, options) => { calls.push([url, options]); return Response.json({ ...session, password: "never-return" }, { headers: { "set-cookie": "mh_session=issued; HttpOnly; Path=/; SameSite=Lax" } }); });
  const response = await POST({ ...request({ ...credentials, private: "drop" }, { host: "app.test", origin: "https://app.test" }), url: "https://localhost:10000/api/auth/login" });
  assert.equal(calls.length, 1); assert.equal(calls[0][0], "http://api.test/v1/auth/login");
  assert.deepEqual(JSON.parse(calls[0][1].body), { ...credentials, login: "User" }); assert.equal(calls[0][1].redirect, "error"); assert.equal(calls[0][1].cache, "no-store"); assert.ok(calls[0][1].signal instanceof AbortSignal);
  assert.deepEqual(await response.json(), { authenticated: true, realm: "seller" }); assert.match(response.headers.get("set-cookie"), /mh_session=issued/); assert.equal(response.headers.get("cache-control"), "no-store");
});

test("login failures are classified without exposing upstream HTML, errors or cookies and never retried", async () => {
  for (const status of [401, 429, 422, 500, 502, 503, 504]) {
    let count = 0; const { POST } = route("../app/api/auth/login/route.ts", async () => { count++; return new Response("<html>password-with-spaces</html>", { status, headers: { "set-cookie": "mh_session=unsafe" } }); });
    const response = await POST(request()); assert.equal(response.status, status === 500 ? 503 : status); assert.equal(response.headers.get("set-cookie"), null); assert.equal((await response.text()).includes("password-with-spaces"), false); assert.equal(count, 1);
  }
});

test("malformed or wrong-realm successful login cannot create a browser session", async () => {
  for (const make of [() => new Response("<html>private</html>", { headers: { "set-cookie": "mh_session=unsafe" } }), () => Response.json({ ...session, realm: "agency" }, { headers: { "set-cookie": "mh_session=unsafe" } }), () => Response.json(session)]) {
    const { POST } = route("../app/api/auth/login/route.ts", async () => make()); const response = await POST(request());
    assert.equal(response.status, 502); assert.equal(response.headers.get("set-cookie"), null);
  }
});

test("bounded login request distinguishes a timeout from a network failure", async () => {
  const signal = new AbortController(); signal.abort();
  const expired = route("../app/api/auth/login/route.ts", async () => { throw new Error("private"); }, { AbortSignal: { timeout: (ms) => { assert.equal(ms, 30000); return signal.signal; } } });
  const offline = route("../app/api/auth/login/route.ts", async () => { throw new Error("private"); });
  assert.equal((await expired.POST(request())).status, 504); assert.equal((await offline.POST(request())).status, 503);
});

test("cold start polls only read-only readiness then stops immediately when ready", async () => {
  let clock = 0, calls = 0, waiting = 0;
  const result = await readiness.waitForLoginReadiness({ signal: new AbortController().signal, now: () => clock, pause: async (ms) => { clock += ms; }, onWaiting: () => waiting++, fetcher: async (url, options) => {
    calls++; assert.equal(url, "/api/auth/readiness"); assert.equal(options.method, "GET"); assert.equal(options.credentials, "omit"); assert.equal(options.body, undefined);
    return calls === 1 ? new Response("<html>waking</html>", { status: 503 }) : Response.json({ ready: true });
  } });
  assert.equal(result, true); assert.equal(calls, 2); assert.equal(waiting, 1); assert.equal(clock, 2000);
});

test("readiness polling keeps a cold-start request open and stops at 180 seconds without credentials", async () => {
  let clock = 0, calls = 0;
  const result = await readiness.waitForLoginReadiness({ signal: new AbortController().signal, now: () => clock, pause: async (ms) => { clock += ms; }, onWaiting() {}, fetcher: async (url, options) => {
    calls++; assert.equal(url, "/api/auth/readiness"); assert.equal(options.credentials, "omit");
    clock += Math.min(60000, 180000 - clock); return Response.json({ ready: false }, { status: 503 });
  } });
  assert.equal(result, false); assert.equal(clock, 180000); assert.equal(calls, 3);
});

test("a 50 second Render cold start completes within one readiness request", async () => {
  let clock = 0, calls = 0;
  const result = await readiness.waitForLoginReadiness({ signal: new AbortController().signal, now: () => clock, pause: async (ms) => { clock += ms; }, onWaiting() {}, fetcher: async (_url, options) => {
    calls++; assert.equal(options.credentials, "omit"); assert.equal(options.body, undefined); clock += 50000;
    return Response.json({ ready: true });
  } });
  assert.equal(result, true); assert.equal(clock, 50000); assert.equal(calls, 1);
});

test("browser does not abort the BFF before its 60 second cold-start window", async () => {
  const timers = [];
  const coldStartReadiness = load("../app/lib/auth-login-readiness.ts", {
    setTimeout(_callback, milliseconds) { timers.push(milliseconds); return timers.length; },
    clearTimeout() {},
  });
  const result = await coldStartReadiness.waitForLoginReadiness({
    signal: new AbortController().signal, now: () => 0, onWaiting() {},
    fetcher: async () => Response.json({ ready: true }),
  });
  assert.equal(result, true); assert.equal(timers[0], 65000);
});

test("unmount cancellation aborts the current readiness request and prevents another poll", async () => {
  const controller = new AbortController(); let called = 0;
  const pending = readiness.waitForLoginReadiness({ signal: controller.signal, onWaiting() {}, fetcher: async (_url, options) => { called++; return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true })); } });
  controller.abort(); await assert.rejects(pending, (error) => error.name === "AbortError"); assert.equal(called, 1);
  await assert.rejects(readiness.abortableDelay(2000, controller.signal), (error) => error.name === "AbortError");
});

test("default readiness polling clock is monotonic and has a Date fallback", async () => {
  let clock = 0, calls = 0;
  const monotonic = load("../app/lib/auth-login-readiness.ts", { performance: { now: () => clock }, Date: { now: () => { throw new Error("wall clock must not be used"); } } });
  const fallback = load("../app/lib/auth-login-readiness.ts", { performance: undefined, Date: { now: () => 7654 } });
  const result = await monotonic.waitForLoginReadiness({
    signal: new AbortController().signal, onWaiting() {}, pause: async (milliseconds) => { clock += milliseconds; },
    fetcher: async () => { calls++; return Response.json({ ready: false }, { status: 503 }); },
  });
  assert.equal(result, false); assert.equal(clock, 180000); assert.equal(calls, 90);
  assert.equal(fallback.monotonicNow(), 7654);
});

test("recovery performs anonymous GET probes until ready", async () => {
  let calls = 0, pauses = 0;
  await readiness.recoverLoginReadiness({
    signal: new AbortController().signal,
    pause: async (milliseconds) => { pauses++; assert.equal(milliseconds, 5000); },
    fetcher: async (url, options) => {
      calls++;
      assert.equal(url, "/api/auth/readiness"); assert.equal(options.method, "GET"); assert.equal(options.credentials, "omit"); assert.equal(options.body, undefined);
      return calls === 1 ? Response.json({ ready: false }, { status: 503 }) : Response.json({ ready: true });
    },
  });
  assert.equal(calls, 2); assert.equal(pauses, 1);
});

function component(wait, fetchImpl, realm = "seller", readinessOverrides = {}) {
  const states = [], refs = [], effects = [], calls = [], navigations = [];
  let stateIndex, refIndex, effectIndex, pendingEffects, tree, refreshes = 0, formDataConstructions = 0;
  class FakeInput { constructor(value) { this.value = value; } }
  const password = new FakeInput(credentials.password);
  const form = { fields: { login: credentials.login, password: credentials.password }, elements: { namedItem: () => password } };
  const defaultRecovery = ({ signal }) => new Promise((_resolve, reject) => {
    if (signal.aborted) { const error = new Error("aborted"); error.name = "AbortError"; reject(error); return; }
    signal.addEventListener("abort", () => { const error = new Error("aborted"); error.name = "AbortError"; reject(error); }, { once: true });
  });
  const module = load("../app/components/LoginForm.tsx", { HTMLInputElement: FakeInput, FormData: class { constructor(value) { formDataConstructions++; this.fields = { ...value.fields }; } get(key) { return this.fields[key]; } }, fetch: async (...args) => { calls.push(args); return fetchImpl(...args); }, require(name) {
    if (name === "react") return {
      useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = initial; return [states[index], (value) => { states[index] = value; }]; },
      useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
      useEffect(create, dependencies) { const index = effectIndex++; if (!effects[index]) pendingEffects.push(() => { effects[index] = { cleanup: create(), dependencies }; }); },
    };
    if (name === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (name === "next/navigation") return { useRouter: () => ({ replace: (url) => navigations.push(url), refresh: () => refreshes++ }) };
    if (name === "../lib/auth-login-contract") return contract;
    if (name === "../lib/auth-login-readiness") return {
      waitForLoginReadiness: wait,
      probeLoginReadiness: readinessOverrides.probe ?? (async () => false),
      recoverLoginReadiness: readinessOverrides.recover ?? defaultRecovery,
    };
    throw new Error(name);
  } });
  const elements = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(elements) : [node, ...elements(node.props?.children)];
  const text = (node) => typeof node === "string" ? node : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  function render() { stateIndex = refIndex = effectIndex = 0; pendingEffects = []; tree = module.LoginForm({ realm, title: "Accesso", description: "Demo" }); pendingEffects.forEach((effect) => effect()); }
  render();
  return { render, calls, navigations, password, form, formDataConstructions: () => formDataConstructions, refreshes: () => refreshes, text: () => text(tree),
    async submit() { const run = elements(tree).find((node) => node.type === "form").props.onSubmit({ preventDefault() {}, currentTarget: form }); render(); await run; render(); },
    async flush() { await new Promise(setImmediate); render(); },
    pending: () => elements(tree).find((node) => node.type === "button").props.disabled,
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}

test("login UI awaits readiness, blocks double submission, posts password once and clears it on confirmed success", async () => {
  let ready; const panel = component(({ onWaiting }) => { onWaiting(); return new Promise((resolve) => { ready = resolve; }); }, async () => Response.json({ authenticated: true, realm: "seller" }));
  const submitted = panel.submit(); assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0); assert.equal(panel.pending(), true); assert.match(panel.text(), /Avvio del servizio in corso/);
  await panel.submit(); ready(true); await submitted;
  assert.equal(panel.formDataConstructions(), 1); assert.equal(panel.calls.length, 1); assert.equal(panel.calls[0][0], "/api/auth/login"); assert.equal(JSON.parse(panel.calls[0][1].body).password, credentials.password); assert.deepEqual(panel.navigations, ["/seller"]); assert.equal(panel.password.value, ""); panel.unmount();
});

test("mount prewarms anonymously and aborts that probe before a submitted readiness check", async () => {
  let prewarmSignal, waitCalls = 0, probeCalls = 0;
  const prewarmHelper = load("../app/lib/auth-login-readiness.ts", { fetch: async (url, options) => {
    probeCalls++; assert.equal(url, "/api/auth/readiness"); assert.equal(options.method, "GET"); assert.equal(options.credentials, "omit"); assert.equal(options.body, undefined);
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true }));
  } });
  const panel = component(async () => { waitCalls++; return false; }, async () => { throw new Error("must not call"); }, "seller", {
    probe: ({ signal }) => { prewarmSignal = signal; return prewarmHelper.probeLoginReadiness({ signal }); },
  });
  assert.ok(prewarmSignal); assert.equal(prewarmSignal.aborted, false); assert.equal(probeCalls, 1); assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0);
  await panel.submit();
  assert.equal(prewarmSignal.aborted, true); assert.equal(waitCalls, 1); assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0); panel.unmount();
});

test("readiness deadline recovers read-only, clears the stale error and requires a new submit", async () => {
  let ready = false, finishRecovery, recoverySignal;
  const panel = component(async () => ready, async () => Response.json({ authenticated: true, realm: "seller" }), "seller", {
    recover: ({ signal }) => { recoverySignal = signal; return new Promise((resolve) => { finishRecovery = resolve; }); },
  });
  await panel.submit();
  assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0); assert.match(panel.text(), /credenziali non sono state inviate/); assert.equal(panel.pending(), false); assert.equal(recoverySignal.aborted, false);
  ready = true; finishRecovery(); await panel.flush();
  assert.doesNotMatch(panel.text(), /credenziali non sono state inviate/); assert.match(panel.text(), /Servizio pronto\. Premi Accedi\./); assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0); assert.deepEqual(panel.navigations, []);
  await panel.submit();
  assert.equal(panel.calls.length, 1); assert.equal(panel.calls[0][0], "/api/auth/login"); assert.equal(panel.formDataConstructions(), 1); assert.deepEqual(panel.navigations, ["/seller"]); panel.unmount();
});

test("unmount aborts readiness recovery and ignores a late ready result", async () => {
  let finishRecovery, recoverySignal;
  const panel = component(async () => false, async () => { throw new Error("must not call"); }, "seller", {
    recover: ({ signal }) => { recoverySignal = signal; return new Promise((resolve) => { finishRecovery = resolve; }); },
  });
  await panel.submit(); panel.unmount();
  assert.equal(recoverySignal.aborted, true); finishRecovery(); await panel.flush();
  assert.equal(panel.calls.length, 0); assert.equal(panel.formDataConstructions(), 0); assert.doesNotMatch(panel.text(), /Servizio pronto\. Premi Accedi\./);
});

test("authentication 401, rate-limit 429 and cold-service HTML remain distinct without automatic password retry", async () => {
  for (const [status, message] of [[401, /Credenziali non valide/], [429, /Troppi tentativi/], [502, /risposta non valida/], [503, /momentaneamente non disponibile/], [504, /non è stato confermato in tempo/]]) {
    const panel = component(async () => true, async () => new Response("<html>private</html>", { status })); await panel.submit();
    assert.equal(panel.calls.length, 1); assert.match(panel.text(), message); assert.equal(panel.text().includes("private"), false); assert.deepEqual(panel.navigations, []); assert.equal(panel.pending(), false); panel.unmount();
  }
});

test("malformed success and wrong realm do not navigate or expose server content", async () => {
  for (const make of [() => new Response("<html>private</html>"), () => Response.json({ authenticated: true, realm: "agency" })]) {
    const panel = component(async () => true, async () => make()); await panel.submit(); assert.deepEqual(panel.navigations, []); assert.match(panel.text(), /risposta non valida/); assert.equal(panel.calls.length, 1); panel.unmount();
  }
});

test("unmount during readiness or password request aborts work and ignores late success", async () => {
  let resume; const waiting = component(({ signal }) => new Promise((resolve) => { resume = () => { assert.equal(signal.aborted, true); resolve(true); }; }), async () => Response.json({ authenticated: true, realm: "seller" }));
  const wake = waiting.submit(); waiting.unmount(); resume(); await wake; assert.equal(waiting.calls.length, 0); assert.deepEqual(waiting.navigations, []);
  let done; const posting = component(async () => true, async () => new Promise((resolve) => { done = resolve; }));
  const post = posting.submit(); await new Promise(setImmediate); posting.unmount(); assert.equal(posting.calls[0][1].signal.aborted, true); done(Response.json({ authenticated: true, realm: "seller" })); await post; assert.deepEqual(posting.navigations, []); assert.equal(posting.calls.length, 1);
});
