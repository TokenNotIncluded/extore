const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/device-login.js"), "utf8");
const accountSource = fs.readFileSync(path.join(__dirname, "../extore/static/account.js"), "utf8");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const ids = { shop: "11111111-1111-4111-8111-111111111111", product: "22222222-2222-4222-8222-222222222222", link: "33333333-3333-4333-8333-333333333333", request: "44444444-4444-4444-8444-444444444444", other: "55555555-5555-4555-8555-555555555555" };
const shopAuth = { role: "admin", shop_id: ids.shop, superadmin: false, session_id: "browser-session-one" };
const rootAuth = { role: "admin", shop_id: null, superadmin: true, session_id: "root-session-one" };
const staffAuth = { role: "staff", shop_id: ids.shop, product_id: ids.product, link_id: ids.link, superadmin: false, session_id: "staff-session-one" };
const code = "ABCD-EFGH-JKLM";
const scope = () => ({ staff_id: ids.link, link_name: "文档 Bot", product_id: ids.product, product_name: "文档 / PPT", shop_id: ids.shop, shop_name: "lightstore", permissions: ["queue.view", "queue.process", "queue.retry"], expires: Date.now() / 1000 + 86400, remaining_cli_uses: 1 });
const details = () => ({ request_id: ids.request, user_code: code, client_name: "我的 Bot", fingerprint: "a".repeat(64), product_id: null, expires: Date.now() / 1000 + 600 });
function options(selected = false, overrides = {}) {
  const grant = overrides.scope || scope();
  return { request: overrides.request || details(), candidates: [grant], ...(selected ? { selected: grant, review_digest: "b".repeat(64) } : {}), ...overrides };
}

function fixture(settings = {}) {
  const nodes = new Map(), requests = [], adopted = [], navigations = [], accounts = [], windowListeners = new Map();
  let current = true;
  const decode = (value) => String(value).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  function element(attributes = "") {
    const children = new Set(), listeners = new Map();
    const node = { attributes, value: decode(attributes.match(/\bvalue="([^"]*)"/)?.[1] || ""), disabled: false, textContent: "", isConnected: true, focusCalls: 0,
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => [...nodes.values()].filter((entry) => entry.isConnected && selector === 'input[type="password"]' && /\btype="password"/.test(entry.attributes)),
      addEventListener(event, handler) { if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(handler); },
      async emit(event) { for (const handler of [...(listeners.get(event) || [])]) await handler({ preventDefault() {} }); },
      focus() { this.focusCalls++; },
      disconnect() { this.isConnected = false; for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); },
    };
    let html = "";
    Object.defineProperty(node, "innerHTML", { get() { return html; }, set(value) {
      for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear();
      html = String(value);
      for (const match of html.matchAll(/<[a-z]+\b([^>]*)>/gi)) {
        const attrs = match[1], id = attrs.match(/\bid="([^"]*)"/)?.[1];
        if (id) { nodes.set("#" + id, element(attrs)); children.add("#" + id); }
      }
      for (const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]*)"[^>]*>([^<]*)<\/\1>/gi)) {
        const child = nodes.get("#" + match[2]); if (child) child.textContent = decode(match[3]);
      }
    } });
    return node;
  }
  const root = element();
  const accountModule = { mount(config) {
    const entry = { config, disposed: false, fresh: null, dispose() { this.disposed = true; }, confirmFresh(handler) { this.fresh = handler; config.root.innerHTML = '<button id="account-fresh-cancel">取消</button>'; } };
    accounts.push(entry); return entry;
  } };
  const window = { location: { origin: settings.origin || "https://extore.example.test" }, ExtoreAccount: accountModule,
    addEventListener(event, listener) { windowListeners.set(event, listener); }, removeEventListener(event) { windowListeners.delete(event); },
  };
  const context = vm.createContext({ window, AbortController, URL, URLSearchParams, Date, Set, WeakMap, setTimeout, clearTimeout, console });
  if (settings.realAccount) {
    vm.runInContext(accountSource, context);
    const actualAccount = window.ExtoreAccount;
    window.ExtoreAccount = { ...actualAccount, mount(config) {
      const original = actualAccount.mount(config), entry = { config, disposed: false, fresh: null };
      accounts.push(entry);
      return { dispose() { entry.disposed = true; original.dispose(); }, confirmFresh(handler, title) { entry.fresh = handler; return original.confirmFresh(handler, title); } };
    } };
  }
  vm.runInContext(source, context);
  const auth = settings.auth ?? staffAuth;
  const instance = window.ExtoreDeviceLogin.mount({ root, auth, language: settings.language || "zh-CN", isCurrent: () => current, passkey: settings.passkey || (() => {}), onAuth: (value) => adopted.push(value), navigate: (url) => navigations.push(url),
    api(url, body, method, extra) { return new Promise((resolve, reject) => requests.push({ url, body, method, extra, resolve, reject })); },
  });
  return { root, instance, ui: window.ExtoreDeviceLogin, requests, accounts, adopted, navigations, node: (selector) => nodes.get(selector), leave() { current = false; }, pagehide() { windowListeners.get("pagehide")?.(); }, auth };
}
const resolve = async (p, index, value) => { assert.ok(p.requests[index], `Missing request ${index}`); p.requests[index].resolve(value); await flush(); };
async function enter(p, value = options()) {
  p.node("#device-code").value = code;
  const pending = p.node("#device-code-form").emit("submit");
  await resolve(p, 0, p.auth);
  assert.equal(p.requests[1].url, "/manage/device/options");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[1].body)), { user_code: code });
  await resolve(p, 1, value); await pending;
}
async function review(p, value = options(true)) {
  await enter(p, { request: value.request, candidates: value.candidates });
  p.node("#device-scope").value = value.selected.staff_id;
  const pending = p.node("#device-scope-form").emit("submit");
  await resolve(p, 2, p.auth);
  await resolve(p, 3, value); await pending;
  return value;
}
async function approvePrelude(p, value) {
  const pending = p.node("#device-approve").emit("click");
  await resolve(p, 4, p.auth);
  await resolve(p, 5, value);
  return { pending };
}

test("device code is visible grouped text and never sent or authorized automatically", async () => {
  const p = fixture();
  assert.match(p.root.innerHTML, /type="text"/);
  assert.match(p.root.innerHTML, /maxlength="14"/);
  assert.match(p.root.innerHTML, /陌生人/);
  assert.equal(p.requests.length, 0);
  assert.equal(p.ui.normalizeCode(" abcd-efgh-jklm "), "ABCDEFGHJKLM");
  p.node("#device-code").value = "https://secret.example/link";
  await p.node("#device-code-form").emit("submit");
  assert.equal(p.requests.length, 0);
  assert.match(p.node("#device-error").textContent, /12 位/);
  await review(p);
  assert.equal(p.requests.filter((request) => request.url.endsWith("/approve")).length, 0);
  assert.match(p.root.innerHTML, /设备指纹/);
  assert.match(p.root.innerHTML, /queue\.view/);
  assert.match(p.root.innerHTML, /queue\.process/);
  assert.match(p.root.innerHTML, /queue\.retry/);
  assert.match(p.root.innerHTML, /将使用 1 次 CLI/);
  assert.match(p.root.innerHTML, /a{64}/);
});

test("staff approval checks scope and review again and confirms only explicit server success", async () => {
  const p = fixture(), value = await review(p);
  const { pending } = await approvePrelude(p, value);
  await flush(); await resolve(p, 6, p.auth); await resolve(p, 7, value);
  assert.equal(p.requests[8].url, "/manage/device/approve");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[8].body)), { user_code: code, staff_id: ids.link, review_digest: "b".repeat(64) });
  assert.equal(p.requests[8].extra.expectedSessionId, staffAuth.session_id);
  assert.equal(p.requests[8].extra.expectedScope, ids.shop);
  await resolve(p, 8, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
  assert.match(p.root.innerHTML, /回到 CLI/);
  assert.doesNotMatch(p.root.innerHTML, /a{64}|ABCDEFGH|grant_token|bearer|access_token/);
});

test("permission, expiry or device changes require another explicit decision", async () => {
  for (const change of ["permissions", "expires", "client_name", "digest"]) {
    const p = fixture(), value = await review(p);
    const newer = JSON.parse(JSON.stringify(value));
    if (change === "permissions") newer.selected.permissions = [...newer.selected.permissions, "product.edit"];
    if (change === "expires") newer.selected.expires -= 20;
    if (change === "client_name") newer.request.client_name = "另一台设备";
    if (change === "digest") newer.review_digest = "c".repeat(64);
    newer.candidates = [newer.selected];
    const { pending } = await approvePrelude(p, newer); await flush(); await pending;
    assert.equal(p.requests.length, 6);
    assert.equal(p.instance.state, "review");
    assert.match(p.node("#device-error").textContent, /已改变/);
    assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
  }
});

test("owner approval requires fresh authentication and adopts only the same authority", async () => {
  const p = fixture({ auth: shopAuth }), value = await review(p);
  await approvePrelude(p, value); await flush();
  assert.equal(p.instance.state, "confirm");
  assert.equal(p.accounts.length, 1);
  assert.equal(p.accounts[0].config.mode, "confirm");
  assert.equal(p.requests.length, 6);
  const fresh = { ...shopAuth, session_id: "fresh-session-two" };
  p.accounts[0].config.onAuth(fresh);
  const pending = p.accounts[0].fresh();
  assert.equal(p.requests[6].extra.expectedSessionId, "fresh-session-two");
  await resolve(p, 6, fresh); await resolve(p, 7, value);
  await resolve(p, 8, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
  assert.equal(p.accounts[0].disposed, true);
  const q = fixture({ auth: rootAuth }), next = await review(q);
  await approvePrelude(q, next); await flush();
  assert.throws(() => q.accounts[0].config.onAuth(shopAuth), /scope changed/);
  assert.equal(q.requests.length, 6);
});

test("owner fresh authentication cancellation restores review without approval", async () => {
  const p = fixture({ auth: shopAuth }), value = await review(p);
  await approvePrelude(p, value); await flush();
  await p.node("#account-fresh-cancel").emit("click");
  assert.equal(p.instance.state, "review");
  assert.equal(p.requests.length, 6);
  assert.equal(p.accounts[0].disposed, true);
  assert.match(p.node("#device-error").textContent, /已取消/);
  assert.equal(p.node("#device-approve").disabled, false);
});

test("changing the review after fresh owner verification stops approval", async () => {
  const p = fixture({ auth: shopAuth }), value = await review(p);
  await approvePrelude(p, value); await flush();
  const pending = p.accounts[0].fresh();
  await resolve(p, 6, p.auth);
  const newer = JSON.parse(JSON.stringify(value)); newer.review_digest = "d".repeat(64);
  await resolve(p, 7, newer); await pending;
  assert.equal(p.instance.state, "review");
  assert.equal(p.requests.length, 8);
  assert.match(p.node("#device-error").textContent, /已改变/);
});

test("deny has separate explicit action and never exposes a device credential", async () => {
  const p = fixture(), value = await review(p);
  const pending = p.node("#device-deny").emit("click");
  await resolve(p, 4, p.auth);
  assert.equal(p.requests[5].url, "/manage/device/deny");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[5].body)), { user_code: code });
  await resolve(p, 5, { ok: true, status: "denied" }); await pending;
  assert.equal(p.instance.state, "denied");
  assert.match(p.root.innerHTML, /未占用 CLI/);
  assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
});

test("cross-product staff scopes, cross-shop merchant scopes and quota-less scopes are rejected", async () => {
  for (const settings of [
    { auth: staffAuth, change: { staff_id: ids.other } },
    { auth: staffAuth, change: { product_id: ids.other } },
    { auth: shopAuth, change: { shop_id: ids.other } },
    { auth: staffAuth, change: { remaining_cli_uses: 0 } },
  ]) {
    const p = fixture({ auth: settings.auth }), invalid = options();
    invalid.candidates[0] = { ...invalid.candidates[0], ...settings.change };
    await enter(p, invalid);
    assert.equal(p.instance.state, "code");
    assert.equal(p.requests.length, 2);
    assert.match(p.node("#device-error").textContent, /身份或授权范围/);
    assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
  }
});

test("metadata is escaped and all selected permissions must match candidate and product filter", async () => {
  const p = fixture(), value = options(true);
  value.request.client_name = '<img onerror="secret">';
  value.selected.product_name = '<svg onload="bad">';
  await review(p, value);
  assert.doesNotMatch(p.root.innerHTML, /<img onerror|<svg onload/);
  assert.match(p.root.innerHTML, /&lt;img onerror/);
  for (const mismatched of ["filter", "candidate"]) {
    const q = fixture(), invalid = options(true);
    if (mismatched === "filter") invalid.request.product_id = ids.other;
    else invalid.candidates = [{ ...invalid.selected, permissions: ["queue.view"] }];
    await enter(q, { request: invalid.request, candidates: invalid.candidates });
    q.node("#device-scope").value = ids.link;
    const pending = q.node("#device-scope-form").emit("submit");
    await resolve(q, 2, q.auth); await resolve(q, 3, invalid); await pending;
    assert.equal(q.instance.state, "scope");
    assert.equal(q.requests.length, 4);
    assert.match(q.node("#device-error").textContent, /范围/);
  }
});

test("session changes and late responses cannot approve or replace another route", async () => {
  const p = fixture(), value = await review(p);
  const pending = p.node("#device-approve").emit("click");
  await resolve(p, 4, { ...p.auth, session_id: "different-session" }); await pending;
  assert.equal(p.requests.length, 5);
  assert.match(p.node("#device-error").textContent, /身份或授权范围/);
  const q = fixture(); q.node("#device-code").value = code;
  const reading = q.node("#device-code-form").emit("submit");
  await resolve(q, 0, q.auth);
  q.instance.dispose(); q.root.innerHTML = "Other route";
  assert.equal(q.requests[1].extra.signal.aborted, true);
  await resolve(q, 1, options()); await reading;
  assert.equal(q.root.innerHTML, "Other route");
  const r = fixture({ auth: shopAuth }), next = await review(r);
  await approvePrelude(r, next); await flush();
  const callback = r.accounts[0].fresh;
  r.instance.dispose(); r.root.innerHTML = "Other route";
  await callback();
  assert.equal(r.requests.length, 6);
  assert.equal(r.root.innerHTML, "Other route");
});

test("embedded sign-in preserves code in page memory and still requires a new review", async () => {
  const p = fixture({ auth: {} });
  p.node("#device-code").value = code;
  await p.node("#device-code-form").emit("submit");
  assert.equal(p.requests.length, 0);
  assert.equal(p.instance.state, "login");
  assert.equal(p.accounts[0].config.mode, "login");
  assert.equal(p.accounts[0].config.token, undefined);
  const pending = p.accounts[0].config.navigate("/admin");
  await resolve(p, 0, shopAuth); await pending;
  assert.equal(p.instance.state, "code");
  assert.equal(p.node("#device-code").value, code);
  assert.equal(p.requests.length, 1);
  assert.deepEqual(p.navigations, []);
  assert.equal(p.accounts[0].disposed, true);
  assert.equal(p.adopted[0].session_id, shopAuth.session_id);
});

test("API failure or unexpected success cannot present false authorization", async () => {
  const p = fixture(), value = await review(p);
  const { pending } = await approvePrelude(p, value); await flush();
  await resolve(p, 6, p.auth); await resolve(p, 7, value);
  await resolve(p, 8, { ok: true, status: "denied" }); await pending;
  assert.equal(p.instance.state, "review");
  assert.match(p.node("#device-error").textContent, /结果尚未确认/);
  assert.doesNotMatch(p.root.innerHTML, /<h2>已授权/);
  const q = fixture(); q.node("#device-code").value = code;
  const lookup = q.node("#device-code-form").emit("submit");
  await resolve(q, 0, q.auth);
  q.requests[1].reject(new Error("Too many device code lookups")); await lookup;
  assert.match(q.node("#device-error").textContent, /太频繁/);
});

test("HTTPS is required outside local development and pagehide disposes pending mounts", () => {
  const p = fixture({ origin: "http://extore.example.test" });
  assert.equal(p.instance.state, "invalid");
  assert.match(p.root.innerHTML, /HTTPS/);
  assert.equal(p.requests.length, 0);
  const local = fixture({ origin: "http://127.0.0.1:8077", language: "en" });
  assert.equal(local.instance.state, "code");
  assert.match(local.root.innerHTML, /Authorize a CLI device/);
  local.pagehide();
  assert.equal(local.instance.active, false);
});

test("existing same-key CLI device can be restored with zero remaining bindings", async () => {
  const p = fixture(), value = options(true);
  value.selected.already_bound = true; value.selected.remaining_cli_uses = 0;
  await review(p, value);
  assert.match(p.root.innerHTML, /恢复已绑定的 CLI 设备，不新增绑定次数/);
  assert.doesNotMatch(p.root.innerHTML, /批准将使用 1 次/);
  const { pending } = await approvePrelude(p, value);
  await resolve(p, 6, p.auth); await resolve(p, 7, value);
  await resolve(p, 8, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
});

test("real Account Passkey confirmation preserves updated session and authorization lifecycle", async () => {
  const fresh = { ...shopAuth, session_id: "fresh-real-account-session" };
  const p = fixture({ auth: shopAuth, realAccount: true, passkey: async () => fresh }), value = await review(p);
  await approvePrelude(p, value);
  assert.equal(p.instance.state, "confirm");
  assert.equal(p.requests.length, 6);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  assert.equal(p.requests[6].url, "/auth/status");
  assert.equal(p.requests[6].extra.expectedSessionId, fresh.session_id);
  const sessionBoundReview = { ...value, review_digest: "e".repeat(64) };
  await resolve(p, 6, fresh); await resolve(p, 7, sessionBoundReview);
  assert.equal(p.requests[8].body.review_digest, "e".repeat(64));
  await resolve(p, 8, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
  assert.equal(p.accounts[0].disposed, true);
});

test("real Account password confirmation clears factors and requires unchanged browser scope", async () => {
  const p = fixture({ auth: shopAuth, realAccount: true }), value = await review(p);
  await approvePrelude(p, value);
  const passwordInput = p.node("#fresh-password"), codeInput = p.node("#fresh-code");
  passwordInput.value = "private-password"; codeInput.value = "123456";
  const pending = p.node("#account-fresh-form").emit("submit"); await flush();
  assert.equal(p.requests[6].url, "/auth/reauth/password");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[6].body)), { password: "private-password", code: "123456" });
  await resolve(p, 6, { ok: true });
  assert.equal(passwordInput.value, ""); assert.equal(codeInput.value, "");
  await resolve(p, 7, p.auth); await resolve(p, 8, p.auth); await resolve(p, 9, value);
  await resolve(p, 10, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
});

test("real Account cancellation aborts in-flight Passkey without a later approval", async () => {
  let finishVerification, capturedSignal;
  const p = fixture({ auth: shopAuth, realAccount: true, passkey: (_register, _name, extra) => { capturedSignal = extra.signal; return new Promise((resolve) => { finishVerification = resolve; }); } }), value = await review(p);
  await approvePrelude(p, value);
  const verifying = p.node("#account-fresh-passkey").emit("click"); await flush();
  await p.node("#account-fresh-cancel").emit("click");
  assert.equal(capturedSignal.aborted, true);
  finishVerification(shopAuth); await verifying;
  assert.equal(p.instance.state, "review");
  assert.equal(p.requests.length, 6);
  assert.match(p.node("#device-error").textContent, /已取消/);
});

test("real Passkey session rotation still stops changed device or permission metadata", async () => {
  const fresh = { ...shopAuth, session_id: "fresh-changed-session" };
  const p = fixture({ auth: shopAuth, realAccount: true, passkey: async () => fresh }), value = await review(p);
  await approvePrelude(p, value);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  const changedReview = JSON.parse(JSON.stringify(value));
  changedReview.review_digest = "f".repeat(64);
  changedReview.selected.permissions.push("cards.manage");
  await resolve(p, 6, fresh); await resolve(p, 7, changedReview); await pending;
  assert.equal(p.instance.state, "review");
  assert.equal(p.requests.length, 8);
  assert.match(p.node("#device-error").textContent, /已改变/);
  assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
});
