const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/pipeline-authorizations.js"), "utf8");
const accountSource = fs.readFileSync(path.join(__dirname, "../extore/static/account.js"), "utf8");
const flush = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const ids = { shop: "11111111-1111-4111-8111-111111111111", product: "22222222-2222-4222-8222-222222222222", grant: "33333333-3333-4333-8333-333333333333", other: "44444444-4444-4444-8444-444444444444" };
const shopAuth = { role: "admin", shop_id: ids.shop, superadmin: false, session_id: "browser-session-one" };
const rootAuth = { role: "admin", shop_id: null, superadmin: true, session_id: "root-session-one" };
const row = (extra = {}) => ({ id: ids.grant, shop_id: ids.shop, shop_name: "lightstore", kind: "shop.pipeline", permissions: ["queue.view", "queue.process", "queue.retry"], product_ids: [ids.product], products: [{ id: ids.product, name: "文档 / PPT", mode: "manual" }], expires: Date.now() / 1000 + 86400, revision: 1, created: Date.now() / 1000 - 100, last_seen: Date.now() / 1000 - 10, client_name: "我的 Bot", fingerprint: "a".repeat(64), revoked: false, issuer_role: "shop", issuer_shop_id: ids.shop, bindings_count: 1, ...extra });
const shops = () => [{ id: ids.shop, name: "lightstore", enabled: true }, { id: ids.other, name: "停用的店铺", enabled: false }];

function fixture(settings = {}) {
  const nodes = new Map(), requests = [], accounts = [], adopted = [], copies = [], windowListeners = new Map();
  let current = true;
  const decode = (value) => String(value).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  function element(attributes = "") {
    const children = new Set(), listeners = new Map();
    const node = { attributes, value: decode(attributes.match(/\bvalue="([^"]*)"/)?.[1] || ""), disabled: /\bdisabled(?:\s|$)/.test(attributes), textContent: "", isConnected: true, open: false,
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => [...nodes.values()].filter((entry) => entry.isConnected && selector === 'input[type="password"]' && /\btype="password"/.test(entry.attributes)),
      addEventListener(event, handler) { if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(handler); },
      async emit(event) { for (const handler of [...(listeners.get(event) || [])]) await handler({ preventDefault() {} }); },
      focus() {},
      disconnect() { this.isConnected = false; for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); },
    };
    let html = "";
    Object.defineProperty(node, "innerHTML", { get() { return html; }, set(value) {
      for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); html = String(value);
      for (const match of html.matchAll(/<[a-z]+\b([^>]*)>/gi)) {
        const attrs = match[1], id = attrs.match(/\bid="([^"]*)"/)?.[1];
        if (id) { nodes.set("#" + id, element(attrs)); children.add("#" + id); }
      }
      for (const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]*)"[^>]*>([^<]*)<\/\1>/gi)) {
        const child = nodes.get("#" + match[2]); if (child) child.textContent = decode(match[3]);
      }
      for (const match of html.matchAll(/<select\b[^>]*\bid="([^"]*)"[^>]*>([\s\S]*?)<\/select>/gi)) {
        const child = nodes.get("#" + match[1]); if (child) child.value = decode(match[2].match(/<option\b[^>]*value="([^"]*)"/)?.[1] || "");
      }
      if (html.startsWith("<option")) this.value = decode(html.match(/\bvalue="([^"]*)"/)?.[1] || "");
    } });
    return node;
  }
  const root = element();
  const mockAccount = { mount(config) {
    const entry = { config, disposed: false, fresh: null, dispose() { this.disposed = true; }, confirmFresh(handler) { this.fresh = handler; config.root.innerHTML = '<button id="account-fresh-cancel">取消</button>'; } };
    accounts.push(entry); return entry;
  } };
  const window = { ExtoreAccount: mockAccount, addEventListener(event, handler) { windowListeners.set(event, handler); }, removeEventListener(event) { windowListeners.delete(event); } };
  const context = vm.createContext({ window, AbortController, URLSearchParams, Date, Set, WeakMap, setTimeout, clearTimeout, console });
  if (settings.realAccount) {
    vm.runInContext(accountSource, context);
    const actualAccount = window.ExtoreAccount;
    window.ExtoreAccount = { ...actualAccount, mount(config) {
      const original = actualAccount.mount(config), entry = { config, disposed: false, fresh: null }; accounts.push(entry);
      return { dispose() { entry.disposed = true; original.dispose(); }, confirmFresh(handler, title) { entry.fresh = handler; return original.confirmFresh(handler, title); } };
    } };
  }
  vm.runInContext(source, context);
  const api = (url, body, method, extra) => new Promise((resolve, reject) => requests.push({ url, body, method, extra, resolve, reject }));
  const config = { root, api, auth: settings.auth || shopAuth, isCurrent: () => current, language: settings.language || "zh-CN", passkey: settings.passkey || (() => {}), onAuth: (value) => adopted.push(value), copyPrompt: (...args) => { copies.push(args); return settings.copyResult; } };
  const instance = window.ExtorePipelineAuthorizations.mount(config);
  return { root, instance, requests, accounts, adopted, copies, config, ui: window.ExtorePipelineAuthorizations, node: (selector) => nodes.get(selector), leave() { current = false; }, pagehide() { windowListeners.get("pagehide")?.(); } };
}
const resolve = async (p, index, value) => { assert.ok(p.requests[index], `Missing request ${index}`); p.requests[index].resolve(value); await flush(); };
async function ready(p, value = row()) { await resolve(p, 0, [value]); if (p.config.auth.superadmin) await resolve(p, 1, shops()); }
async function review(p, value = row()) { await ready(p, value); await p.node("#pipeline-revoke-" + value.id).emit("click"); }
async function confirmation(p, value = row()) {
  await review(p, value);
  const pending = p.node("#pipeline-confirm-revoke").emit("click");
  await resolve(p, p.config.auth.superadmin ? 2 : 1, p.config.auth); await pending;
  assert.equal(p.instance.phase, "confirm");
  return value;
}

test("defaults to active access and pins the owner session and shop without requesting credentials", async () => {
  const p = fixture(), value = row();
  assert.equal(p.requests.length, 1);
  assert.equal(p.requests[0].url, "/admin/pipeline-authorizations?view=active&limit=200");
  assert.equal(p.requests[0].extra.expectedScope, ids.shop);
  assert.equal(p.requests[0].extra.expectedSessionId, shopAuth.session_id);
  assert.equal(p.requests[0].method, "GET");
  await ready(p, value);
  assert.match(p.root.innerHTML, /新增商品需再次批准/);
  assert.match(p.node("#pipeline-list").innerHTML, /a{64}|queue\.process|文档 \/ PPT/);
  assert.equal(p.instance.view, "active");
  assert.equal(p.node("#pipeline-history").open, false);
  assert.equal(p.requests.some((request) => /ticket|approve|authorization_link/.test(request.url)), false);
});

test("historical grants remain hidden until explicitly requested, and active view can be restored", async () => {
  const p = fixture(); await ready(p);
  p.node("#pipeline-history-view").value = "revoked";
  const history = p.node("#pipeline-history-load").emit("click");
  assert.equal(p.requests[1].url, "/admin/pipeline-authorizations?view=revoked&limit=200");
  await resolve(p, 1, [row({ revoked: true })]); await history;
  assert.equal(p.instance.view, "revoked");
  assert.match(p.node("#pipeline-list").innerHTML, /已撤销/);
  assert.equal(p.node("#pipeline-revoke-" + ids.grant), undefined);
  p.node("#pipeline-history-view").value = "all";
  const all = p.node("#pipeline-history-load").emit("click");
  assert.match(p.requests[2].url, /view=all/); await resolve(p, 2, []); await all;
  const active = p.node("#pipeline-active").emit("click");
  assert.match(p.requests[3].url, /view=active/); await resolve(p, 3, []); await active;
  assert.equal(p.instance.view, "active");
  assert.match(p.node("#pipeline-list").innerHTML, /暂无有效/);
});

test("active view suppresses revoked and expired metadata even if a response contains history", async () => {
  const p = fixture();
  await resolve(p, 0, [row(), row({ id: ids.other, client_name: "已撤销的设备", revoked: true }), row({ id: ids.product, client_name: "已到期的设备", expires: Date.now() / 1000 - 1 })]);
  assert.doesNotMatch(p.node("#pipeline-list").innerHTML, /已撤销的设备|已到期的设备/);
  assert.match(p.node("#pipeline-list-status").textContent, /1$/);
});

test("a full 500-product snapshot remains auditable without rejecting the backend limit", async () => {
  const p = fixture(), value = row();
  value.products = Array.from({ length: 500 }, (_, index) => ({ id: `55555555-5555-4555-8555-${index.toString(16).padStart(12, "0")}`, name: `流水线商品 ${index + 1}`, mode: "manual" }));
  value.product_ids = value.products.map((product) => product.id); value.bindings_count = 500;
  await ready(p, value);
  assert.match(p.node("#pipeline-list").innerHTML, /500 个商品|流水线商品 500/);
  assert.equal(p.node("#pipeline-error").textContent, "");
});

test("a historical authorization remains auditable after its product bindings have been cleaned", async () => {
  const p = fixture(); await ready(p);
  p.node("#pipeline-history-view").value = "all";
  const pending = p.node("#pipeline-history-load").emit("click");
  await resolve(p, 1, [row({ kind: "product", product_ids: [], products: [], bindings_count: 0, revoked: true })]); await pending;
  assert.match(p.node("#pipeline-list").innerHTML, /0 个商品|已撤销/);
  assert.equal(p.node("#pipeline-error").textContent, "");
});

test("copying an owner pipeline prompt makes no API request and requests only pipeline permissions", async () => {
  const p = fixture(); await ready(p);
  await p.node("#pipeline-copy-prompt").emit("click");
  assert.equal(p.requests.length, 1); assert.equal(p.copies.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(p.copies[0][0])), { shopId: ids.shop, allPipelines: true, permissions: ["queue.view", "queue.process", "queue.retry"] });
  assert.equal(p.copies[0][1], p.node("#pipeline-prompt-host"));
  assert.equal(p.copies[0][2](), true); p.leave(); assert.equal(p.copies[0][2](), false);
});

test("platform administrators must choose an enabled shop before copying and cannot invent a shop", async () => {
  const p = fixture({ auth: rootAuth }); await ready(p);
  assert.equal(p.requests[1].url, "/platform/shops");
  assert.equal(p.requests[0].extra.expectedScope, "platform");
  assert.equal(p.node("#pipeline-copy-prompt").disabled, true);
  await p.node("#pipeline-copy-prompt").emit("click"); assert.equal(p.copies.length, 0);
  p.node("#pipeline-shop").value = ids.shop;
  const selected = p.node("#pipeline-shop").emit("change");
  assert.equal(p.requests[2].url, `/admin/pipeline-authorizations?view=active&shop_id=${ids.shop}&limit=200`);
  await resolve(p, 2, [row()]); await selected;
  await p.node("#pipeline-copy-prompt").emit("click"); assert.equal(p.requests.length, 3); assert.equal(p.copies[0][0].shopId, ids.shop);
  p.node("#pipeline-shop").value = ids.other;
  const disabledShop = p.node("#pipeline-shop").emit("change"); await resolve(p, 3, []); await disabledShop;
  assert.equal(p.node("#pipeline-copy-prompt").disabled, true);
  p.node("#pipeline-shop").value = ids.product;
  await p.node("#pipeline-shop").emit("change");
  assert.equal(p.requests.length, 4); assert.equal(p.copies.length, 1);
});

test("metadata is escaped and server scopes cannot cross the owner's shop", async () => {
  const p = fixture(), value = row({ client_name: '<img onerror="secret">', shop_name: "<svg>" });
  value.products[0].name = '<a onclick="bad">'; await ready(p, value);
  assert.doesNotMatch(p.node("#pipeline-list").innerHTML, /<img|<svg>|<a onclick/);
  assert.match(p.node("#pipeline-list").innerHTML, /&lt;img|&lt;svg|&lt;a/);
  const q = fixture(); await resolve(q, 0, [row({ shop_id: ids.other, issuer_shop_id: ids.other })]);
  assert.equal(q.node("#pipeline-list").innerHTML, ""); assert.match(q.node("#pipeline-error").textContent, /身份或店铺/);
  assert.equal(q.accounts.length, 0);
});

test("staff, CLI and unauthenticated sessions cannot mount or refresh owner authorization management", async () => {
  for (const auth of [{}, { ...shopAuth, role: "staff" }, { ...shopAuth, channel: "cli" }]) {
    const p = fixture({ auth }); await p.instance.refresh();
    assert.equal(p.requests.length, 0); assert.match(p.root.innerHTML, /浏览器会话/);
  }
});

test("late list replies are ignored after leaving, disposing or replacing the mount", async () => {
  for (const action of ["leave", "dispose", "pagehide", "replace"]) {
    const p = fixture();
    if (action === "leave") p.leave();
    if (action === "dispose") p.instance.dispose();
    if (action === "pagehide") p.pagehide();
    if (action === "replace") p.ui.mount({ ...p.config, auth: {} });
    const before = p.root.innerHTML;
    await resolve(p, 0, [row({ client_name: "晚到的回复" })]);
    assert.equal(p.root.innerHTML, before);
    if (action !== "leave") assert.equal(p.requests[0].extra.signal.aborted, true);
  }
});

test("revocation requires a separate explicit review and can be cancelled before verification", async () => {
  const p = fixture(); await review(p);
  assert.equal(p.requests.length, 1); assert.equal(p.accounts.length, 0);
  assert.match(p.node("#pipeline-revoke-review").innerHTML, /释放回队列/);
  assert.match(p.node("#pipeline-revoke-review").innerHTML, /queue\.retry|a{64}/);
  await p.node("#pipeline-cancel-revoke").emit("click");
  assert.equal(p.instance.phase, "list"); assert.equal(p.requests.length, 1);
  assert.equal(p.node("#pipeline-revoke-review").innerHTML, "");
});

test("a replaced browser session or owner identity stops revocation before fresh authentication", async () => {
  for (const current of [{ ...shopAuth, session_id: "other-session" }, { ...shopAuth, shop_id: ids.other }, rootAuth]) {
    const p = fixture(); await review(p);
    const pending = p.node("#pipeline-confirm-revoke").emit("click");
    await resolve(p, 1, current); await pending;
    assert.equal(p.accounts.length, 0); assert.equal(p.requests.length, 2);
    assert.match(p.node("#pipeline-error").textContent, /身份或店铺/);
  }
});

test("fresh authentication adopts only a verified rotation of the same authority and revokes exact scope", async () => {
  const p = fixture(), value = row(); await confirmation(p, value);
  const fresh = { ...shopAuth, session_id: "fresh-browser-session" };
  p.accounts[0].config.onAuth(fresh);
  const pending = p.accounts[0].fresh();
  assert.equal(p.requests[2].extra.expectedSessionId, fresh.session_id);
  await resolve(p, 2, fresh);
  assert.equal(p.requests[3].url, "/admin/pipeline-authorizations?view=all&limit=200");
  await resolve(p, 3, [{ ...value, last_seen: value.last_seen + 10 }]);
  assert.equal(p.requests[4].url, "/admin/pipeline-authorizations/" + ids.grant);
  assert.equal(p.requests[4].method, "DELETE"); assert.deepEqual(JSON.parse(JSON.stringify(p.requests[4].body)), { expected_revision: value.revision });
  assert.equal(p.requests[4].extra.expectedSessionId, fresh.session_id);
  await resolve(p, 4, { ok: true, id: ids.grant, revoked_bindings: 1, released_jobs: 2 });
  assert.equal(p.instance.phase, "list"); assert.equal(p.accounts[0].disposed, true);
  assert.match(p.node("#pipeline-notice").textContent, /1.*2/);
  await resolve(p, 5, []); await pending;
  assert.equal(p.adopted.length, 1); assert.deepEqual(p.adopted[0], fresh);
});

test("changed permissions, products, expiry, revision, device or issuer require a new explicit revocation review", async () => {
  for (const field of ["permissions", "products", "expires", "revision", "client_name", "fingerprint", "issuer_role"]) {
    const p = fixture(), value = row(); await confirmation(p, value);
    const pending = p.accounts[0].fresh(); await resolve(p, 2, shopAuth);
    const changed = JSON.parse(JSON.stringify(value));
    if (field === "permissions") changed.permissions = ["queue.view"];
    if (field === "products") { changed.product_ids.push(ids.other); changed.products.push({ id: ids.other, name: "新的商品", mode: "manual" }); changed.bindings_count = 2; }
    if (field === "expires") changed.expires += 10;
    if (field === "revision") changed.revision++;
    if (field === "client_name") changed.client_name = "新的设备名";
    if (field === "fingerprint") changed.fingerprint = "c".repeat(64);
    if (field === "issuer_role") { changed.issuer_role = "root"; changed.issuer_shop_id = null; }
    await resolve(p, 3, [changed]); await pending;
    assert.equal(p.instance.phase, "review"); assert.equal(p.requests.length, 4);
    assert.equal(p.accounts[0].disposed, true); assert.match(p.node("#pipeline-error").textContent, /已改变/);
    assert.equal(p.requests.some((request) => request.method === "DELETE"), false);
  }
});

test("cancelling a fresh verification or leaving prevents its late handler from revoking", async () => {
  for (const action of ["cancel", "dispose", "leave"]) {
    const p = fixture(); await confirmation(p);
    if (action === "cancel") await p.node("#account-fresh-cancel").emit("click");
    if (action === "dispose") p.instance.dispose();
    if (action === "leave") p.leave();
    await p.accounts[0].fresh();
    assert.equal(p.requests.length, 2);
    if (action === "cancel") { assert.equal(p.instance.phase, "review"); assert.match(p.node("#pipeline-error").textContent, /已取消/); }
  }
});

test("fresh verification cannot adopt another shop or convert a root identity to shop ownership", async () => {
  for (const settings of [{ auth: shopAuth, changed: { ...shopAuth, shop_id: ids.other } }, { auth: rootAuth, changed: shopAuth }]) {
    const p = fixture({ auth: settings.auth }); await confirmation(p);
    assert.throws(() => p.accounts[0].config.onAuth(settings.changed), /scope changed/);
    assert.equal(p.adopted.length, 0); assert.equal(p.requests.some((request) => request.method === "DELETE"), false);
  }
});

test("unconfirmed revocation responses never display success and keep an explicit retry review", async () => {
  const p = fixture(), value = row(); await confirmation(p, value);
  const pending = p.accounts[0].fresh(); await resolve(p, 2, shopAuth); await resolve(p, 3, [value]);
  await resolve(p, 4, { ok: false, id: ids.grant, revoked_bindings: 1, released_jobs: 0 }); await pending;
  assert.equal(p.instance.phase, "review"); assert.equal(p.node("#pipeline-notice").textContent, "");
  assert.match(p.node("#pipeline-error").textContent, /未完成/);
  assert.equal(p.requests.length, 5);
});

test("an upgrade between reviewing metadata and DELETE is version-pinned and restores review on conflict", async () => {
  const p = fixture(), value = row({ revision: 7 }); await confirmation(p, value);
  const pending = p.accounts[0].fresh(); await resolve(p, 2, shopAuth); await resolve(p, 3, [value]);
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[4].body)), { expected_revision: 7 });
  p.requests[4].reject(new Error("Authorization revision changed (409)")); await flush(); await pending;
  assert.equal(p.instance.phase, "review"); assert.equal(p.accounts[0].disposed, true);
  assert.match(p.node("#pipeline-error").textContent, /版本已改变.*未撤销.*重新核对/);
  assert.equal(p.node("#pipeline-notice").textContent, "");
  assert.equal(p.requests.length, 5);
  assert.equal(p.requests.filter((request) => request.method === "DELETE").length, 1);
});

test("real account Passkey verification keeps the same session for pinned revoke requests", async () => {
  const fresh = { ...shopAuth };
  const p = fixture({ realAccount: true, passkey: async () => fresh }), value = row(); await confirmation(p, value);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  assert.equal(p.requests[2].url, "/auth/status"); assert.equal(p.requests[2].extra.expectedSessionId, fresh.session_id);
  await resolve(p, 2, fresh); await resolve(p, 3, [value]);
  await resolve(p, 4, { ok: true, id: ids.grant, revoked_bindings: 1, released_jobs: 0 });
  await resolve(p, 5, []); await pending;
  assert.equal(p.instance.phase, "list"); assert.equal(p.adopted[0].session_id, fresh.session_id);
});

test("real account password reauthentication keeps the pending revoke pinned and clears password fields", async () => {
  const p = fixture({ realAccount: true }), value = row(); await confirmation(p, value);
  const password = p.node("#fresh-password"); password.value = "fixture-only-password";
  const pending = p.node("#account-fresh-form").emit("submit");
  assert.equal(p.requests[2].url, "/auth/reauth/password"); assert.equal(p.requests[2].body.password, "fixture-only-password");
  await resolve(p, 2, { ok: true }); assert.equal(password.value, "");
  await resolve(p, 3, shopAuth); await resolve(p, 4, shopAuth); await resolve(p, 5, [value]);
  await resolve(p, 6, { ok: true, id: ids.grant, revoked_bindings: 1, released_jobs: 0 }); await resolve(p, 7, []); await pending;
  assert.equal(p.instance.phase, "list"); assert.equal(p.adopted.length, 0);
});

test("revoked or missing latest scopes are refreshed without issuing a duplicate DELETE", async () => {
  const p = fixture(); await confirmation(p);
  const pending = p.accounts[0].fresh(); await resolve(p, 2, shopAuth); await resolve(p, 3, []); await resolve(p, 4, []); await pending;
  assert.equal(p.instance.phase, "list"); assert.match(p.node("#pipeline-notice").textContent, /已失效|已撤销/);
  assert.equal(p.requests.some((request) => request.method === "DELETE"), false);
});

test("English labels and long metadata use responsive text controls without embedding secrets", async () => {
  const p = fixture({ language: "en" }); await ready(p);
  assert.match(p.root.innerHTML, /AI pipeline access|New products require another approval/);
  assert.doesNotMatch(p.root.innerHTML + p.node("#pipeline-list").innerHTML, /authorization_link|access_token|private_key|bearer|grant_token/);
});
