const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/commerce-connect.js"), "utf8");
const flush = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); };
const ids = { shop: "11111111-1111-4111-8111-111111111111", product: "22222222-2222-4222-8222-222222222222", otherProduct: "33333333-3333-4333-8333-333333333333", client: "44444444-4444-4444-8444-444444444444", grant: "55555555-5555-4555-8555-555555555555", otherShop: "66666666-6666-4666-8666-666666666666", request: "A".repeat(43) };
const ownerAuth = () => ({ role: "admin", shop_id: ids.shop, superadmin: false, session_id: "browser-one", shop_name: "lightstore", shop_email: "owner@example.test" });
const rootAuth = () => ({ role: "admin", shop_id: null, superadmin: true, session_id: "root-one" });
const definition = () => ({ request: { id: ids.request, client_id: ids.client, client_name: "Example marketplace", redirect_uri: "https://market.example.test/connect/callback?flow=shop", redirect_host: "market.example.test", scopes: ["products.read", "cards.issue"], requested_product_ids: [], expires: Date.now() / 1000 + 600, max_grant_expires: Date.now() / 1000 + 90 * 86400 }, shop: { id: ids.shop, name: "lightstore" }, products: [{ id: ids.product, name: "Document service", variants: [{ id: "basic", name: "Basic", enabled: true }, { id: "plus", name: "Plus", enabled: true }, { id: "disabled", name: "Old", enabled: false }] }, { id: ids.otherProduct, name: "Image service", variants: [{ id: "default", name: "Default", enabled: true }] }], review_digest: "a".repeat(64) });
const client = (overrides = {}) => ({ id: ids.client, client_id: ids.client, client_name: "Example marketplace", shop_id: ids.shop, redirect_uris: ["https://market.example.test/connect/callback?flow=shop"], token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], created_at: Date.now() / 1000, enabled: true, ...overrides });
const grant = (overrides = {}) => ({ id: ids.grant, client_id: ids.client, client_name: "Example marketplace", shop_id: ids.shop, scopes: ["products.read", "cards.issue"], product_ids: [ids.product], products: [{ id: ids.product, name: "Document service" }], card_limits: [{ product_id: ids.product, variant_id: "basic", max_count: 10, issued_count: 2, remaining: 8 }], expires: Date.now() / 1000 + 86400, revoked: false, ...overrides });

function fixture(options = {}) {
  const nodes = new Map(), requests = [], fresh = [], navigations = [], copied = [], windowEvents = new Map();
  let auth = options.auth ?? ownerAuth(), current = true;
  const decode = (value) => String(value).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  function element(attributes = "") {
    const listeners = new Map(), children = new Set();
    const node = { attributes, value: decode(attributes.match(/\bvalue="([^"]*)"/)?.[1] || ""), disabled: /\bdisabled\b/.test(attributes), checked: /\bchecked\b/.test(attributes), hidden: /\bhidden\b/.test(attributes), textContent: "", isConnected: true, dataset: {}, focused: false, selected: false,
      querySelector(selector) { return nodes.get(selector) || null; },
      querySelectorAll(selector) { const selectors = selector.split(",").map((value) => value.trim()); return [...nodes.values()].filter((value) => value.isConnected && selectors.some((part) => /^\[data-[a-z-]+\]$/.test(part) && value.attributes.includes(part.slice(1, -1)))); },
      addEventListener(event, callback) { if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(callback); },
      async emit(event) { for (const callback of listeners.get(event) || []) await callback({ preventDefault() {} }); },
      focus() { node.focused = true; }, select() { node.selected = true; }, scrollIntoView() {},
      disconnect() { node.isConnected = false; for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); },
    };
    for (const entry of attributes.matchAll(/\bdata-([a-z-]+)="([^"]*)"/g)) node.dataset[entry[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = decode(entry[2]);
    let html = "";
    Object.defineProperty(node, "innerHTML", { get() { return html; }, set(value) {
      for (const key of children) { nodes.get(key)?.disconnect(); nodes.delete(key); } children.clear(); html = String(value);
      for (const entry of html.matchAll(/<[a-z]+\b([^>]*)>/gi)) { const id = entry[1].match(/\bid="([^"]*)"/)?.[1]; if (id) { nodes.set("#" + id, element(entry[1])); children.add("#" + id); } }
    } });
    return node;
  }
  const root = element();
  const window = { location: { origin: "https://extore.example.test" }, addEventListener(event, callback) { windowEvents.set(event, callback); }, removeEventListener(event) { windowEvents.delete(event); }, ExtoreClipboard: { async writeText(value) { copied.push(value); return options.copyResult !== false; } } };
  const context = vm.createContext({ window, navigator: {}, URL, URLSearchParams, AbortController, structuredClone, Date, Set, WeakMap, Event });
  vm.runInContext(source, context);
  const module = window.ExtoreCommerceConnect;
  const instance = module.mount({ root, api(url, body, method, extra) { return new Promise((resolve, reject) => requests.push({ url, body, method, extra, resolve, reject })); }, auth: () => auth, mode: options.mode || "management", requestId: options.requestId || ids.request, language: options.language || "zh-CN", isCurrent: () => current, navigate: (url) => navigations.push(url), onAuth: (value) => { auth = value; }, reauthenticate(config) { return new Promise((resolve, reject) => fresh.push({ config, resolve, reject })); } });
  const node = (name) => [...nodes.entries()].find(([key]) => key.endsWith("-" + name))?.[1] || null;
  return { root, instance, module, node, nodes, requests, fresh, navigations, copied, auth: () => auth, setAuth(value) { auth = value; }, leave() { current = false; }, pagehide() { windowEvents.get("pagehide")?.(); } };
}
async function resolve(page, index, value) { assert.ok(page.requests[index], `Missing request ${index}`); page.requests[index].resolve(value); await flush(); }
async function load(page, clients = [], grants = []) { await resolve(page, 0, { clients }); await resolve(page, 1, { grants }); }
async function authorization(options = {}, value = definition()) { const page = fixture({ mode: "authorize", ...options }); await resolve(page, 0, value); return { page, value }; }
async function choose(page, { issue = false, basic = "10", plus = "10" } = {}) {
  page.node("product-0").checked = true;
  if (issue) page.node("scope-1").checked = true;
  await page.node("product-0").emit("change");
  if (issue) { page.node("limit-0").value = basic; page.node("limit-1").value = plus; }
  await page.node("selection").emit("submit");
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("management loads only own-shop client and grant metadata, without cards or credentials", async () => {
  const page = fixture(); await load(page, [client()], [grant()]);
  assert.equal(page.requests.length, 2);
  assert.equal(page.requests[0].url, "/admin/commerce/clients?shop_id=" + ids.shop + "&view=active");
  assert.equal(page.requests[1].url, "/admin/commerce/grants?shop_id=" + ids.shop + "&view=active");
  for (const request of page.requests) { assert.equal(request.method, "GET"); assert.equal(request.extra.expectedScope, ids.shop); assert.equal(request.extra.expectedSessionId, "browser-one"); assert.ok(request.extra.signal); }
  assert.match(page.root.innerHTML, /lightstore|owner@example.test/);
  assert.doesNotMatch(page.root.innerHTML, /\sstyle\s*=|on(?:click|change|input)=|client_secret|access_token/);
});

test("signed-out, staff and CLI identities cannot load authorization or issue approval requests", async () => {
  for (const auth of [{}, { role: "staff", shop_id: ids.shop, session_id: "staff-one" }, { ...ownerAuth(), channel: "cli" }]) {
    const page = fixture({ mode: "authorize", auth });
    assert.equal(page.requests.length, 0); assert.equal(page.fresh.length, 0);
    assert.match(page.root.innerHTML, /登录店主账号/);
    assert.match(page.root.innerHTML, /return_to=.*connect/);
  }
});

test("platform administrator chooses one enabled shop before any client or request reads", async () => {
  const page = fixture({ auth: rootAuth() });
  assert.equal(page.requests[0].url, "/platform/shops");
  await resolve(page, 0, [{ id: ids.shop, name: "lightstore", enabled: true }, { id: ids.otherShop, name: "Disabled", enabled: false }]);
  assert.equal(page.requests.length, 1); assert.doesNotMatch(page.root.innerHTML, /Disabled/);
  page.node("shop").value = ids.shop; const pending = page.node("shop").emit("change");
  await resolve(page, 1, { clients: [] }); await resolve(page, 2, { grants: [] }); await pending;
  assert.match(page.requests[1].url, new RegExp("shop_id=" + ids.shop));
  assert.equal(page.requests[1].extra.expectedScope, "platform");
});

test("authorization explicitly identifies unverified app, callback and shop, defaults issuance off", async () => {
  const { page } = await authorization();
  assert.equal(page.requests.length, 1); assert.equal(page.fresh.length, 0);
  assert.match(page.root.innerHTML, /未验证.*企业身份/); assert.match(page.root.innerHTML, /market\.example\.test/);
  assert.equal(page.node("product-0").checked, false); assert.equal(page.node("product-1").checked, false);
  assert.equal(page.node("scope-0").checked, true); assert.equal(page.node("scope-1").checked, false);
  assert.equal(page.node("limits").hidden, true);
  assert.doesNotMatch(page.root.innerHTML, /client_secret|access_token|refresh_token/);
});

test("review does not authorize and narrowed read-only approval requires fresh identity", async () => {
  const { page, value } = await authorization(); await choose(page);
  assert.equal(page.requests.length, 1); assert.equal(page.fresh.length, 0);
  assert.match(page.root.innerHTML, /仅导入资料/); assert.doesNotMatch(page.root.innerHTML, /Image service/);
  const pending = page.node("approve").emit("click"); await flush();
  assert.equal(page.fresh.length, 1); assert.equal(page.requests.length, 1);
  page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 1, page.auth()); await resolve(page, 2, value);
  assert.deepEqual(plain(page.requests[3].body.scopes), ["products.read"]);
  assert.deepEqual(plain(page.requests[3].body.product_ids), [ids.product]);
  assert.deepEqual(plain(page.requests[3].body.card_limits), []);
  await resolve(page, 3, { ok: true, redirect_uri: value.request.redirect_uri + "&code=short-code&state=my-state&iss=https%3A%2F%2Fextore.example.test" }); await pending;
  assert.equal(page.navigations.length, 1); assert.doesNotMatch(page.root.innerHTML, /short-code|my-state/);
});

test("per-variant limits are explicit, disabled variants excluded, zero narrows issuance", async () => {
  const { page, value } = await authorization(); await choose(page, { issue: true, basic: "3", plus: "0" });
  assert.match(page.root.innerHTML, /Basic/); assert.doesNotMatch(page.root.innerHTML, /Plus|Old/);
  const pending = page.node("approve").emit("click"); page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 1, page.auth()); await resolve(page, 2, value);
  assert.deepEqual(plain(page.requests[3].body.card_limits), [{ product_id: ids.product, variant_id: "basic", max_count: 3 }]);
  await resolve(page, 3, { ok: true, redirect_uri: value.request.redirect_uri + "&code=short-code&state=s&iss=https%3A%2F%2Fextore.example.test" }); await pending;
});

test("invalid quota, empty scope, empty product and excessive expiry cannot reach verification", async () => {
  for (const invalid of ["no-products", "no-scopes", "negative", "fraction", "too-many", "no-limits", "expiry"]) {
    const { page } = await authorization();
    page.node("product-0").checked = invalid !== "no-products";
    page.node("scope-0").checked = invalid !== "no-scopes";
    page.node("scope-1").checked = ["negative", "fraction", "too-many", "no-limits"].includes(invalid);
    await page.node("product-0").emit("change");
    if (page.node("limit-0")) { page.node("limit-0").value = { negative: "-1", fraction: "1.5", "too-many": "10001", "no-limits": "0" }[invalid] || "0"; page.node("limit-1").value = "0"; }
    if (invalid === "expiry") page.node("expires").value = "2099-01-01T00:00";
    await page.node("selection").emit("submit");
    assert.equal(page.node("approve"), null, invalid); assert.equal(page.fresh.length, 0, invalid); assert.equal(page.requests.length, 1, invalid);
    assert.match(page.node("error").textContent, /请选择/);
  }
});

test("adjusting reviewed scope preserves product choices, reduced quotas and expiry", async () => {
  const { page } = await authorization(); await choose(page, { issue: true, basic: "2", plus: "0" });
  await page.node("adjust").emit("click");
  assert.equal(page.node("product-0").checked, true); assert.equal(page.node("product-1").checked, false);
  assert.equal(page.node("scope-1").checked, true); assert.equal(page.node("limit-0").value, "2"); assert.equal(page.node("limit-1").value, "0");
  await page.node("selection").emit("submit"); assert.match(page.root.innerHTML, /Basic/); assert.doesNotMatch(page.root.innerHTML, /Plus/);
});

test("cancelled fresh verification never sends approval, denial or registration", async () => {
  const { page } = await authorization(); await choose(page);
  const pending = page.node("approve").emit("click"); page.fresh[0].resolve(false); await pending;
  assert.equal(page.requests.length, 1); assert.equal(page.navigations.length, 0); assert.match(page.node("status").textContent, /已取消/);
});

test("fresh Passkey can rotate same-account session and API requests use the verified session", async () => {
  const { page, value } = await authorization(); await choose(page);
  const pending = page.node("approve").emit("click"); const updated = { ...page.auth(), session_id: "browser-two" };
  page.setAuth(updated); page.fresh[0].config.onAuth(updated); page.fresh[0].resolve(updated); await flush();
  await resolve(page, 1, updated); await resolve(page, 2, value);
  assert.equal(page.requests[1].extra.expectedSessionId, "browser-two"); assert.equal(page.requests[3].extra.expectedSessionId, "browser-two");
  await resolve(page, 3, { ok: true, redirect_uri: value.request.redirect_uri + "&code=c&state=s&iss=https%3A%2F%2Fextore.example.test" }); await pending;
  assert.equal(page.navigations.length, 1);
});

test("changed products or client metadata forces a new review instead of approval", async () => {
  const { page, value } = await authorization(); await choose(page);
  const pending = page.node("approve").emit("click"); page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 1, page.auth());
  const changed = structuredClone(value); changed.products[0].name = "Changed product"; changed.review_digest = "b".repeat(64);
  await resolve(page, 2, changed); await pending;
  assert.equal(page.requests.length, 3); assert.equal(page.navigations.length, 0); assert.match(page.node("error").textContent, /已变化/);
  assert.equal(page.node("product-0").checked, false);
});

test("denial uses fresh identity and exact server-bound access_denied callback", async () => {
  const { page, value } = await authorization(); await page.node("deny").emit("click");
  assert.equal(page.requests.length, 1); assert.equal(page.fresh.length, 0);
  const pending = page.node("deny-confirm").emit("click"); page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 1, page.auth()); await resolve(page, 2, value);
  assert.match(page.requests[3].url, /\/deny\?shop_id=/); assert.deepEqual(plain(page.requests[3].body), { shop_id: ids.shop, review_digest: value.review_digest });
  await resolve(page, 3, { ok: true, redirect_uri: value.request.redirect_uri + "&error=access_denied&state=s&iss=https%3A%2F%2Fextore.example.test" }); await pending;
  assert.equal(page.navigations.length, 1); assert.match(page.root.innerHTML, /已拒绝连接/);
});

test("callback validator rejects substituted origin, path, query, fragment and invalid result", () => {
  const page = fixture({ auth: {} }), bound = "https://market.example.test/connect/callback?flow=shop", good = bound + "&code=c&state=s&iss=https%3A%2F%2Fextore.example.test";
  assert.equal(page.module.safeRedirect({ ok: true, redirect_uri: good }, bound), good);
  for (const destination of [good.replace("market.example.test", "evil.example.test"), good.replace("/connect/callback", "/other"), good.replace("flow=shop", "flow=other"), good + "#secret", good + "&code=second", good + "&evil=redirect", "javascript:alert(1)", good.replace("https://", "http://"), good.replace("&code=c", "&error=access_denied")]) assert.throws(() => page.module.safeRedirect({ ok: true, redirect_uri: destination }, bound));
  assert.throws(() => page.module.safeRedirect({ ok: false, redirect_uri: good }, bound));
  assert.throws(() => page.module.safeRedirect({ ok: true, redirect_uri: good }, bound, "denied"));
});

test("authorization context rejects invalid request locator and cross-shop or contradictory metadata", () => {
  const page = fixture({ auth: {} }), good = definition(); assert.equal(page.module.context(good, ids.shop, ids.request).shop.id, ids.shop);
  for (const mutate of [(value) => { value.shop.id = ids.otherShop; }, (value) => { value.request.redirect_host = "evil.example.test"; }, (value) => { value.request.scopes.push("cards.manage"); }, (value) => { value.request.requested_product_ids = [ids.otherShop]; }, (value) => { value.products.push(value.products[0]); }, (value) => { value.products[0].variants[0].id = "<script>"; }, (value) => { value.request.expires = 0; }, (value) => { value.review_digest = "unknown"; }]) { const value = structuredClone(good); mutate(value); assert.throws(() => page.module.context(value, ids.shop, ids.request)); }
  assert.throws(() => page.module.context(good, ids.shop, ids.grant));
  const defaultPort = structuredClone(good); defaultPort.request.redirect_uri = "https://MARKET.EXAMPLE.TEST:443/connect/callback?flow=shop"; defaultPort.request.redirect_host = "MARKET.EXAMPLE.TEST:443";
  assert.equal(page.module.context(defaultPort, ids.shop, ids.request).request.redirect_host, "MARKET.EXAMPLE.TEST:443");
});

test("hostile application and product names are escaped, never followed or executed", async () => {
  const value = definition(); value.request.client_name = '<img src=x onerror="steal()">'; value.products[0].name = '<script>https://evil.example.test</script>';
  const { page } = await authorization({}, value);
  assert.match(page.root.innerHTML, /&lt;img/); assert.match(page.root.innerHTML, /&lt;script/); assert.doesNotMatch(page.root.innerHTML, /<img src=x|<script>https/);
  assert.equal(page.navigations.length, 0); assert.equal(page.requests.length, 1);
});

test("client registration contains only public PKCE metadata and is not posted before fresh confirmation", async () => {
  const page = fixture(); await load(page);
  page.node("client-name").value = "My shop platform"; page.node("redirects").value = "https://market.example.test/connect/callback";
  const pending = page.node("register").emit("submit"); assert.equal(page.requests.length, 2); assert.equal(page.fresh.length, 1);
  page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 2, page.auth());
  assert.deepEqual(plain(page.requests[3].body), { client_name: "My shop platform", redirect_uris: ["https://market.example.test/connect/callback"], token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] });
  await resolve(page, 3, { client: client({ client_name: "My shop platform", redirect_uris: ["https://market.example.test/connect/callback"] }) });
  await resolve(page, 4, { clients: [client()] }); await resolve(page, 5, { grants: [] }); await pending;
  assert.match(page.node("status").textContent, /已注册/);
});

test("invalid callbacks are rejected before any fresh verification or client mutation", async () => {
  for (const callback of ["http://market.example.test/callback", "https://user:pass@market.example.test/callback", "https://market.example.test/callback#fragment", "https://market.example.test/callback?code=abc", "https://127.0.0.1/callback", "https://market.example.test/callback\nhttps://market.example.test/callback"]) {
    const page = fixture(); await load(page); page.node("client-name").value = "Marketplace"; page.node("redirects").value = callback;
    await page.node("register").emit("submit"); assert.equal(page.requests.length, 2); assert.equal(page.fresh.length, 0); assert.ok(page.node("error").textContent);
  }
});

test("public client identifier copy falls back to selection without exposing credentials", async () => {
  const page = fixture({ copyResult: false }); await load(page, [client()]);
  await page.node("client-copy-0").emit("click");
  assert.equal(page.copied[0], ids.client); assert.equal(page.node("client-id-0").focused, true); assert.equal(page.node("client-id-0").selected, true);
  assert.match(page.node("status").textContent, /手动复制/); assert.equal(page.requests.length, 2);
});

test("revocation uses inline review and fresh identity, then reloads active grants", async () => {
  const page = fixture(); await load(page, [client()], [grant()]);
  await page.node("grant-revoke-0").emit("click"); assert.equal(page.fresh.length, 0); assert.equal(page.requests.length, 2);
  const pending = page.node("revoke-confirm").emit("click"); page.fresh[0].resolve(page.auth()); await flush(); await resolve(page, 2, page.auth());
  assert.equal(page.requests[3].method, "DELETE"); assert.match(page.requests[3].url, new RegExp("/grants/" + ids.grant));
  await resolve(page, 3, { ok: true }); await resolve(page, 4, { clients: [client()] }); await resolve(page, 5, { grants: [] }); await pending;
  assert.match(page.node("status").textContent, /已撤销/);
});

test("revoked clients and completed grant history are hidden until explicit history selection", async () => {
  const page = fixture(); const revoked = client({ client_id: ids.otherShop, id: ids.otherShop, client_name: "Old client", enabled: false });
  await load(page, [client(), revoked], [grant(), grant({ id: ids.otherShop, client_name: "Old grant", revoked: true })]);
  assert.doesNotMatch(page.root.innerHTML, /Old client|Old grant/);
  page.node("history").checked = true; const pending = page.node("history").emit("change");
  assert.match(page.requests[3].url, /view=all/);
  await resolve(page, 2, { clients: [client(), revoked] }); await resolve(page, 3, { grants: [grant({ id: ids.otherShop, client_name: "Old grant", revoked: true })] }); await pending;
  assert.match(page.root.innerHTML, /Old client|Old grant/);
});

test("late loads and fresh results cannot modify another page or changed identity", async () => {
  for (const change of ["dispose", "identity", "session", "replace", "leave", "pagehide"]) {
    const page = fixture();
    if (change === "dispose") page.instance.dispose();
    if (change === "identity") page.setAuth({ ...page.auth(), shop_id: ids.otherShop });
    if (change === "session") page.setAuth({ ...page.auth(), session_id: "unverified-session" });
    if (change === "replace") page.root.innerHTML = "Another page";
    if (change === "leave") page.leave();
    if (change === "pagehide") page.pagehide();
    const markup = page.root.innerHTML; await resolve(page, 0, { clients: [client()] }); await resolve(page, 1, { grants: [] }); assert.equal(page.root.innerHTML, markup, change); assert.equal(page.instance.active, false, change);
  }
  const { page } = await authorization(); await choose(page); const pending = page.node("approve").emit("click"); page.root.innerHTML = "Other page"; page.fresh[0].resolve(page.auth()); await pending;
  assert.equal(page.requests.length, 1); assert.equal(page.navigations.length, 0); assert.equal(page.root.innerHTML, "Other page");
});

test("English copy and mobile styling remain independent of theme and CSP", async () => {
  const { page } = await authorization({ language: "en" }); assert.match(page.root.innerHTML, /Connect a marketplace|Read product details/);
  const css = fs.readFileSync(path.join(__dirname, "../extore/static/commerce-connect.css"), "utf8");
  assert.match(css, /var\(--paper\)|var\(--white\)/); assert.match(css, /min-height: 44px/); assert.match(css, /font-size: 16px/); assert.match(css, /max-width: 1100px/);
  assert.doesNotMatch(page.root.innerHTML, /\sstyle\s*=|onclick=/);
});

test("expired, unauthenticated and rate-limited requests explain recovery without echoing private errors", async () => {
  for (const [status, message, expected] of [[409, "授权申请无效、已到期或已处理", /重新发起连接/], [401, "PRIVATE_SERVER_ERROR", /重新确认店主身份/], [429, "PRIVATE_SERVER_ERROR", /稍后重试/], [403, "PRIVATE_SERVER_ERROR", /对应店主账号/]]) {
    const page = fixture({ mode: "authorize" }); const failure = new Error(message); failure.status = status;
    page.requests[0].reject(failure); await flush();
    assert.match(page.node("error").textContent, expected); assert.doesNotMatch(page.root.innerHTML, /PRIVATE_SERVER_ERROR/); assert.equal(page.navigations.length, 0);
  }
});
