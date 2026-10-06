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
  return { request: overrides.request || details(), candidates: [grant], ...(selected ? { selected: grant, review_digest: "b".repeat(64), snapshot_digest: "9".repeat(64) } : {}), ...overrides };
}

function fixture(settings = {}) {
  const nodes = new Map(), requests = [], adopted = [], navigations = [], accounts = [], windowListeners = new Map();
  let current = true;
  const decode = (value) => String(value).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  function element(attributes = "") {
    const children = new Set(), listeners = new Map();
    const node = { attributes, value: decode(attributes.match(/\bvalue="([^"]*)"/)?.[1] || ""), disabled: /\bdisabled\b/.test(attributes), checked: /\bchecked\b/.test(attributes), dataset: {}, textContent: "", isConnected: true, focusCalls: 0,
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => [...nodes.values()].filter((entry) => entry.isConnected && (selector === 'input[type="password"]' ? /\btype="password"/.test(entry.attributes) : selector === "[data-device-selection]" ? entry.attributes.includes("data-device-selection") : /^input\[name="[a-z-]+"\]$/.test(selector) && entry.attributes.includes(selector.slice(6, -1)))),
      addEventListener(event, handler) { if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(handler); },
      async emit(event) { for (const handler of [...(listeners.get(event) || [])]) await handler({ preventDefault() {} }); },
      focus() { this.focusCalls++; },
      disconnect() { this.isConnected = false; for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); },
    };
    for (const match of attributes.matchAll(/\bdata-([a-z-]+)="([^"]*)"/g)) node.dataset[match[1].replace(/-([a-z])/g, (_, character) => character.toUpperCase())] = decode(match[2]);
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

function activeOptions({ kind = "shop.pipeline", existing = false, restore = false } = {}) {
  const expires = Date.now() / 1000 + 86400;
  const requested = ["queue.view", "queue.process", "queue.retry"];
  const products = [{ id: ids.product, name: "Word 文档", mode: "manual" }, ...(kind === "shop.pipeline" ? [{ id: ids.other, name: "PPT 演示", mode: "manual" }] : [])];
  const current = existing ? { id: "66666666-6666-4666-8666-666666666666", shop_id: ids.shop, kind, product_ids: [ids.product], permissions: ["queue.view"], expires, revision: 3, client_name: "已绑定 Bot", fingerprint: "a".repeat(64) } : null;
  return { flow: "scope", request: { ...details(), kind, shop_id: ids.shop, requested_product_ids: products.map((product) => product.id), requested_permissions: requested,
    grant_expires: expires, authorization_id: current && !restore ? current.id : null, expected_revision: current && !restore ? current.revision : null, reason: "制作文档与 PPT" },
    shop: { id: ids.shop, name: "lightstore" }, products, current,
    selected: { shop_id: ids.shop, kind, product_ids: products.map((product) => product.id), permissions: requested, expires }, review_digest: "b".repeat(64), snapshot_digest: "9".repeat(64) };
}
async function activeReview(p, value, draft = {}) {
  await enter(p, value);
  const selected = { ...value.selected, ...draft };
  for (const product of value.products) p.node("#device-product-" + product.id).checked = selected.product_ids.includes(product.id);
  for (const permission of value.request.requested_permissions) p.node("#device-permission-" + permission.replaceAll(".", "-")).checked = selected.permissions.includes(permission);
  const pending = p.node("#device-active-selection").emit("submit");
  await resolve(p, 2, p.auth);
  const reviewed = { ...value, selected };
  await resolve(p, 3, reviewed); await pending;
  return reviewed;
}

test("active shop request freezes current products and allows explicit smaller selection", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions();
  const reviewed = await activeReview(p, value, { product_ids: [ids.product], permissions: ["queue.view", "queue.process"] });
  assert.equal(p.instance.state, "review");
  assert.match(p.root.innerHTML, /之后的新商品不会自动/);
  assert.match(p.root.innerHTML, /本次未批准/);
  assert.match(p.root.innerHTML, /未批准的权限/);
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[3].body)), { user_code: code, product_ids: [ids.product], permissions: ["queue.view", "queue.process"], expires: reviewed.selected.expires });
  assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
  await p.node("#device-back").emit("click");
  assert.equal(p.node("#device-product-" + ids.product).checked, true);
  assert.equal(p.node("#device-product-" + ids.other).checked, false);
  assert.equal(p.node("#device-permission-queue-retry").checked, false);
});

test("active scope approval requires owner confirmation and only enters awaiting-claim state", async () => {
  const fresh = { ...shopAuth, session_id: "active-fresh-session" };
  const p = fixture({ auth: shopAuth, realAccount: true, passkey: async () => fresh });
  const value = await activeReview(p, activeOptions());
  await approvePrelude(p, value);
  assert.equal(p.instance.state, "confirm");
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  const latest = { ...value, review_digest: "c".repeat(64) };
  await resolve(p, 6, fresh); await resolve(p, 7, latest);
  assert.equal(p.requests[8].url, "/manage/device/approve");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[8].body)), { user_code: code, product_ids: value.selected.product_ids, permissions: value.selected.permissions, expires: value.selected.expires, review_digest: "c".repeat(64) });
  await resolve(p, 8, { ok: true, status: "approved" }); await pending;
  assert.equal(p.instance.state, "approved");
  assert.match(p.root.innerHTML, /已批准，等待 CLI 领取/);
  assert.match(p.root.innerHTML, /领取后生效/);
  assert.doesNotMatch(p.root.innerHTML, /a{64}|access_token|grant_token|browser-session/);
});

test("upgrade preserves existing products, permissions and exact expiry while additions start unchecked", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions({ existing: true });
  await enter(p, value);
  assert.equal(p.node("#device-product-" + ids.product).checked, true);
  assert.equal(p.node("#device-product-" + ids.product).disabled, true);
  assert.equal(p.node("#device-product-" + ids.other).checked, false);
  assert.equal(p.node("#device-permission-queue-view").checked, true);
  assert.equal(p.node("#device-permission-queue-view").disabled, true);
  assert.equal(p.node("#device-permission-queue-process").checked, false);
  assert.equal(p.node("#device-permission-queue-retry").checked, false);
  assert.equal(p.node("#device-grant-expires").disabled, true);
  assert.match(p.root.innerHTML, /拒绝本次申请，保留原授权/);
  p.node("#device-product-" + ids.other).checked = true;
  p.node("#device-permission-queue-process").checked = true;
  const pending = p.node("#device-active-selection").emit("submit");
  await resolve(p, 2, p.auth);
  const selected = { ...value.selected, permissions: ["queue.view", "queue.process"] };
  await resolve(p, 3, { ...value, selected }); await pending;
  assert.equal(p.requests[3].body.expires, value.current.expires);
  assert.match(p.root.innerHTML, /现有授权，保持不变/);
  assert.match(p.root.innerHTML, /新增商品/);
  assert.match(p.root.innerHTML, /本次新增权限/);
  assert.match(p.root.innerHTML, /已有设备名称/);
});

test("denying upgrade does not revoke or modify existing access", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions({ existing: true });
  await enter(p, value);
  const pending = p.node("#device-deny").emit("click");
  await resolve(p, 2, p.auth);
  assert.equal(p.requests[3].url, "/manage/device/deny");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[3].body)), { user_code: code });
  await resolve(p, 3, { ok: true, status: "denied" }); await pending;
  assert.match(p.root.innerHTML, /原有授权与正在处理的任务保持不变/);
  assert.equal(p.requests.some((request) => /revoke|logout|DELETE|authorization\//.test(request.url)), false);
});

test("single-product delegation is never preselected and explains restricted child-link binding", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions({ kind: "product" });
  value.request.requested_permissions = [...value.request.requested_permissions, "product.edit", "links.delegate"];
  value.selected.permissions = [...value.request.requested_permissions];
  await enter(p, value);
  assert.equal(p.node("#device-permission-links-delegate").checked, false);
  assert.match(p.root.innerHTML, /生成更小权限的管理链接/);
  assert.match(p.root.innerHTML, /最多 1 次浏览器 \/ CLI/);
  assert.doesNotMatch(p.root.innerHTML, /owner\.admin/);
});

test("active requests reject unauthorized, dynamic, cross-shop and non-queue scopes", async () => {
  const cases = [
    (value) => { value.request.kind = "owner.admin"; },
    (value) => { value.request.requested_product_ids = ["*"]; },
    (value) => { value.request.requested_permissions.push("cards.manage"); value.selected.permissions.push("cards.manage"); },
    (value) => { value.products[1].mode = "script"; },
    (value) => { value.products[1].id = value.products[0].id; },
    (value) => { value.shop.id = "66666666-6666-4666-8666-666666666666"; },
    (value) => { value.request.shop_id = ids.other; value.shop.id = ids.other; value.selected.shop_id = ids.other; },
    (value) => { value.products.push({ id: "66666666-6666-4666-8666-666666666666", name: "以后创建的商品", mode: "manual" }); },
  ];
  for (const change of cases) {
    const p = fixture({ auth: shopAuth }), value = activeOptions(); change(value);
    await enter(p, value);
    assert.equal(p.instance.state, "code");
    assert.equal(p.requests.length, 2);
    assert.equal(p.node("#device-active-selection"), undefined);
    assert.match(p.node("#device-error").textContent, /授权范围/);
  }
  const staff = fixture(); await enter(staff, activeOptions());
  assert.equal(staff.instance.state, "code");
  assert.equal(staff.requests.length, 2);
});

test("selected scope cannot expand the owner's draft or remove existing upgrade rights", async () => {
  for (const type of ["extra-permission", "extra-product", "remove-existing", "extend-expiry"]) {
    const p = fixture({ auth: shopAuth }), value = activeOptions({ existing: type === "remove-existing" });
    await enter(p, value);
    for (const product of value.products) p.node("#device-product-" + product.id).checked = true;
    for (const permission of value.request.requested_permissions) p.node("#device-permission-" + permission.replaceAll(".", "-")).checked = true;
    const pending = p.node("#device-active-selection").emit("submit");
    await resolve(p, 2, p.auth);
    const invalid = JSON.parse(JSON.stringify(value));
    if (type === "extra-permission") invalid.selected.permissions.push("cards.manage");
    if (type === "extra-product") invalid.selected.product_ids.push("66666666-6666-4666-8666-666666666666");
    if (type === "remove-existing") invalid.selected.product_ids = [ids.other];
    if (type === "extend-expiry") invalid.selected.expires += 3600;
    await resolve(p, 3, invalid); await pending;
    assert.equal(p.instance.state, "scope");
    assert.equal(p.requests.length, 4);
    assert.match(p.node("#device-error").textContent, /授权范围/);
  }
});

test("scope review changes to current revision, future products, permissions or expiry require a new decision", async () => {
  for (const change of ["current", "new-product", "permissions", "expiry"]) {
    const p = fixture({ auth: shopAuth }), value = await activeReview(p, activeOptions({ existing: true }));
    const newer = JSON.parse(JSON.stringify(value));
    if (change === "current") { newer.current.revision++; newer.request.expected_revision++; }
    if (change === "new-product") { newer.products[1].name = "商品名称已修改"; }
    if (change === "permissions") { newer.request.requested_permissions = ["queue.view", "queue.process"]; newer.selected.permissions = [...newer.request.requested_permissions]; }
    if (change === "expiry") { newer.current.expires -= 3600; newer.selected.expires = newer.current.expires; newer.request.grant_expires = newer.current.expires; }
    const pending = p.node("#device-approve").emit("click");
    await resolve(p, 4, p.auth); await resolve(p, 5, newer); await pending;
    assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
    assert.equal(p.accounts.length, 0);
    assert.equal(p.instance.state, "review");
    assert.match(p.node("#device-error").textContent, /已改变|授权范围/);
  }
});

test("fresh reauthentication cannot silently apply changed scope authorization", async () => {
  const fresh = { ...shopAuth, session_id: "scope-new-session" };
  const p = fixture({ auth: shopAuth, realAccount: true, passkey: async () => fresh }), value = await activeReview(p, activeOptions());
  await approvePrelude(p, value);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  const newer = JSON.parse(JSON.stringify(value)); newer.shop.name = "改名后的店铺"; newer.review_digest = "d".repeat(64);
  await resolve(p, 6, fresh); await resolve(p, 7, newer); await pending;
  assert.equal(p.instance.state, "review");
  assert.equal(p.requests.length, 8);
  assert.match(p.node("#device-error").textContent, /已改变/);
  assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
});

test("scope selection is escaped, dependency checked and cannot authorize after disposal", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions();
  value.products[0].name = '<img onerror="steal">'; value.request.reason = '<script>steal()</script>';
  await enter(p, value);
  assert.doesNotMatch(p.root.innerHTML, /<img onerror|<script>steal/);
  p.node("#device-permission-queue-view").checked = false;
  await p.node("#device-active-selection").emit("submit");
  assert.equal(p.requests.length, 2);
  assert.match(p.node("#device-error").textContent, /需同时选择查看队列/);
  const q = fixture({ auth: shopAuth }), next = await activeReview(q, activeOptions());
  const pending = q.node("#device-approve").emit("click");
  await resolve(q, 4, q.auth);
  q.instance.dispose(); q.root.innerHTML = "Other page";
  await resolve(q, 5, next); await pending;
  assert.equal(q.root.innerHTML, "Other page");
  assert.equal(q.requests.length, 6);
  assert.equal(q.requests[5].extra.signal.aborted, true);
});

test("restoration retains existing scope and old expiry with no implied new grants", async () => {
  const p = fixture({ auth: shopAuth }), value = activeOptions({ existing: true, restore: true });
  value.request.requested_product_ids = [...value.current.product_ids]; value.products = value.products.slice(0, 1);
  value.request.requested_permissions = [...value.current.permissions];
  value.selected.product_ids = [...value.current.product_ids]; value.selected.permissions = [...value.current.permissions];
  await enter(p, value);
  assert.match(p.root.innerHTML, /恢复或追加设备授权/);
  assert.equal(p.node("#device-product-" + ids.product).disabled, true);
  assert.equal(p.node("#device-permission-queue-view").disabled, true);
  assert.equal(p.node("#device-grant-expires").disabled, true);
  assert.equal(p.requests.length, 2);
  const pending = p.node("#device-active-selection").emit("submit");
  await resolve(p, 2, p.auth); await resolve(p, 3, value); await pending;
  assert.match(p.root.innerHTML, /仅恢复当前授权，没有新增商品或权限/);
  assert.match(p.root.innerHTML, /恢复当前授权/);
});

test("scope accepts the backend limit of 500 frozen current products and rejects overflow", async () => {
  for (const count of [201, 500, 501]) {
    const p = fixture({ auth: shopAuth }), value = activeOptions();
    value.products = Array.from({ length: count }, (_, index) => ({ id: `${index.toString(16).padStart(8, "0")}-7777-4777-8777-777777777777`, name: `队列商品 ${index}`, mode: "manual" }));
    value.request.requested_product_ids = value.products.map((product) => product.id);
    value.selected.product_ids = [...value.request.requested_product_ids];
    await enter(p, value);
    assert.equal(p.instance.state, count <= 500 ? "scope" : "code");
    if (count <= 500) assert.match(p.root.innerHTML, /选择上述全部当前商品/);
    else assert.match(p.node("#device-error").textContent, /授权范围/);
    assert.equal(p.requests.length, 2);
  }
});

test("fresh session rotation cannot hide a changed server snapshot in either approval flow", async () => {
  for (const scoped of [false, true]) {
    const fresh = { ...shopAuth, session_id: "snapshot-fresh-session" };
    const p = fixture({ auth: shopAuth, realAccount: true, passkey: async () => fresh });
    const value = scoped ? await activeReview(p, activeOptions()) : await review(p);
    await approvePrelude(p, value);
    const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
    const changedSnapshot = { ...value, review_digest: "c".repeat(64), snapshot_digest: "d".repeat(64) };
    await resolve(p, 6, fresh); await resolve(p, 7, changedSnapshot); await pending;
    assert.equal(p.instance.state, "review");
    assert.equal(p.requests.length, 8);
    assert.match(p.node("#device-error").textContent, /已改变/);
    assert.equal(p.requests.some((request) => request.url.endsWith("/approve")), false);
  }
});
