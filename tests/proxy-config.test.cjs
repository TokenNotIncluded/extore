const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/proxy-config.js"), "utf8");
const SHOP = "12345678-1234-1234-1234-123456789012";
const ROUTE = "a".repeat(32), IDENTITY = "b".repeat(32);
const publicRoute = { route_id: ROUTE, issuer_id: IDENTITY, name: "Issuer <safe>", origin: "https://issuer.example", path: "/", public_key: "A".repeat(43) };
const OLD_IDENTITY = "c".repeat(32), OLD_ROUTE = "d".repeat(32);
const currentIdentity = { id: IDENTITY, shop_id: SHOP, name: "main", public_key: "A".repeat(43), current: true };
const currentRoute = { ...publicRoute, shop_id: SHOP, name: "main", identity_id: IDENTITY, enabled: true, default_issuer: true, archived: false };
const oldIdentity = { ...currentIdentity, id: OLD_IDENTITY, name: "Old issuer", current: false };
const oldRoute = { ...currentRoute, route_id: OLD_ROUTE, issuer_id: OLD_IDENTITY, identity_id: OLD_IDENTITY, name: "Old route", enabled: false, default_issuer: false, archived: true };
const owner = { role: "admin", shop_id: SHOP, shop_name: "Shop", shop_email: "owner@example.test", superadmin: false, session_id: "session" };
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve)); };

function page({ auth = owner, handler, clipboard = true } = {}) {
  const nodes = new Map(), calls = [], copied = [];
  let connected = true, confirmation = true;
  class Element {
    constructor(id) { Object.assign(this, { id, value: "", disabled: false, checked: false, isConnected: true, textContent: "" }); this.listeners = new Map(); this.children = []; }
    set innerHTML(html) {
      this.html = html; this.children.forEach((id) => nodes.delete(id)); this.children = [];
      for (const match of html.matchAll(/<([a-z]+)\b([^>]*)>/g)) {
        const id = match[2].match(/\bid="([^"]+)"/)?.[1]; if (!id) continue;
        const child = new Element(id); nodes.set(id, child); this.children.push(id);
        child.value = match[2].match(/\bvalue="([^"]*)"/)?.[1] || "";
        if (match[1] === "select") child.value = html.slice(match.index).match(/<option value="([^"]*)"/)?.[1] || "";
      }
    }
    get innerHTML() { return this.html || ""; }
    querySelector(selector) { return nodes.get(selector.slice(1)); }
    querySelectorAll() { return [...nodes.values()]; }
    addEventListener(event, listener) { this.listeners.set(event, listener); }
    emit(event) { return this.listeners.get(event)?.({ preventDefault() {} }); }
    focus() { this.focused = true; } select() { this.selected = true; }
  }
  const root = new Element("root"), window = { ExtoreClipboard: { async writeText(text) { copied.push(text); return clipboard; } } };
  const context = vm.createContext({ window, URL, URLSearchParams, AbortController, JSON, navigator: {}, confirm: () => confirmation, location: { origin: "https://merchant.example" } });
  vm.runInContext(source, context);
  const api = async (url, body, method, options) => {
    calls.push({ url, body, method, options });
    if (handler) return handler(url, body, method);
    if (url === "/auth/status") return auth;
    if (url.includes("/identities")) return [];
    if (url.includes("/routes")) return [{ ...publicRoute, shop_id: SHOP, enabled: 1, identity_id: null, default_issuer: 0, private_key: "HIDDEN-KEY" }];
    return [];
  };
  const mount = window.ExtoreProxyConfig.mount({ root, api, auth, isCurrent: () => connected });
  return { root, node: (id) => nodes.get(id), calls, copied, mount, api: window.ExtoreProxyConfig, leave() { connected = false; mount.dispose(); }, cancel() { confirmation = false; } };
}

test("merchant scope loads only its routes and copies exactly public pins", async () => {
  const p = page(); await settle();
  assert.equal(p.calls.length, 2);
  assert.ok(p.calls.every((call) => call.url.includes("shop_id=" + SHOP)));
  assert.match(p.node("proxy-route-list").innerHTML, /Issuer &lt;safe&gt;/);
  assert.doesNotMatch(p.root.innerHTML + p.node("proxy-route-list").innerHTML, /HIDDEN-KEY/);
  await p.node("proxy-export-" + ROUTE).emit("click");
  assert.deepEqual(JSON.parse(p.copied[0]), publicRoute);
  assert.equal(p.calls.length, 2);
});

test("clipboard failure keeps public data selected without exposing credentials", async () => {
  const p = page({ clipboard: false }); await settle();
  await p.node("proxy-export-" + ROUTE).emit("click");
  assert.equal(p.node("proxy-public-config").disabled, false);
  assert.equal(p.node("proxy-public-config").selected, true);
  assert.deepEqual(JSON.parse(p.node("proxy-public-config").value), publicRoute);
  assert.match(p.node("proxy-copy-status").textContent, /手动复制/);
});

test("platform root must select a shop before reading or mutating route data", async () => {
  const rootAuth = { ...owner, shop_id: null, superadmin: true };
  const p = page({ auth: rootAuth, handler(url) { if (url === "/platform/shops") return [{ id: SHOP, name: "Chosen", enabled: true }]; if (url.includes("identities")) return []; if (url.includes("routes")) return []; } });
  await settle(); assert.equal(p.calls.length, 1); assert.equal(p.calls[0].url, "/platform/shops");
  assert.equal(p.node("proxy-import-json").disabled, true);
  p.node("proxy-shop").value = SHOP; await p.node("proxy-shop").emit("change");
  assert.equal(p.calls.length, 3); assert.ok(p.calls.slice(1).every((call) => call.url.includes("shop_id=" + SHOP)));
});

test("import accepts public JSON only and has no arbitrary forwarding URL", async () => {
  const p = page(); await settle();
  for (const origin of ["http://issuer.example", "https://user:pass@issuer.example", "https://issuer.example/path", "https://issuer.example?next=x", "https://issuer.example#code"]) {
    assert.throws(() => p.api.publicRoute({ ...publicRoute, origin }, true));
  }
  assert.throws(() => p.api.publicRoute({ ...publicRoute, private_key: "never" }, true));
  p.node("proxy-import-json").value = JSON.stringify({ ...publicRoute, private_key: "never" });
  await p.node("proxy-import-form").emit("submit");
  assert.equal(p.calls.length, 2); assert.match(p.node("proxy-error").textContent, /六项/);
});

test("disable respects cancellation and pins shop selector in PUT", async () => {
  const p = page(); await settle();
  p.cancel(); await p.node("proxy-toggle-" + ROUTE).emit("click"); assert.equal(p.calls.length, 2);
  const q = page(); await settle(); await q.node("proxy-toggle-" + ROUTE).emit("click");
  const write = q.calls.find((call) => call.method === "PUT");
  assert.equal(write.url, "/admin/proxy/routes/" + ROUTE + "?shop_id=" + SHOP);
  assert.deepEqual(JSON.parse(JSON.stringify(write.body)), { enabled: false });
});

test("foreign route rows are rejected and scoped bot prompt contains no credentials", async () => {
  const p = page({ handler(url) { return url.includes("identities") ? [] : [{ ...publicRoute, shop_id: "foreign", enabled: true, private_key: "HIDDEN" }]; } });
  await settle(); assert.match(p.node("proxy-error").textContent, /当前店铺/); assert.equal(p.node("proxy-route-list").innerHTML, "");
  const text = p.api.prompt({ origin: "https://merchant.example", shopId: SHOP });
  assert.match(text, /extore admin proxy routes import/); assert.match(text, new RegExp(SHOP));
  assert.doesNotMatch(text, /HIDDEN|Bearer|private_key|authorization_link|receipt#/);
});

test("leaving during scope revalidation prevents a delayed write", async () => {
  let resolve;
  const p = page({ handler(url) { if (url === "/auth/status") return new Promise((done) => { resolve = done; }); if (url.includes("identities")) return []; return [{ ...publicRoute, shop_id: SHOP, enabled: 1 }]; } });
  await settle(); const updating = p.node("proxy-toggle-" + ROUTE).emit("click"); await settle(); p.leave(); resolve(owner); await updating;
  assert.equal(p.calls.filter((call) => call.method === "PUT").length, 0);
});

test("the mandatory main identity stays current while old identities and routes are disclosed as history", async () => {
  const p = page({ handler(url) {
    if (url.includes("/identities")) return [currentIdentity, oldIdentity];
    if (url.includes("/routes")) return [currentRoute, oldRoute];
  } });
  await settle();
  assert.ok(p.calls.every((call) => call.url.endsWith("&history=true")));
  assert.match(p.node("proxy-identities").innerHTML, /main · 当前标识/);
  assert.doesNotMatch(p.node("proxy-identities").innerHTML, /Old issuer|proxy-delete-identity/);
  assert.match(p.node("proxy-history-identities").innerHTML, /Old issuer/);
  assert.match(p.node("proxy-route-list").innerHTML, /新卡发行路由/);
  assert.doesNotMatch(p.node("proxy-route-list").innerHTML, /Old route|proxy-toggle|proxy-delete-route/);
  assert.match(p.node("proxy-history-routes").innerHTML, /Old route/);
  assert.match(p.node("proxy-history-count").textContent, /1 个标识 \/ 1 条路由/);
  assert.equal(p.node("proxy-identity-name").value, "main");
});

test("replacing main requires confirmation and a fresh shop session without exporting private material", async () => {
  const handler = (url, body, method) => {
    if (url === "/auth/status") return owner;
    if (url.includes("/identities")) return method === "POST" ? {} : [currentIdentity];
    if (url.includes("/routes")) return [currentRoute];
  };
  const cancelled = page({ handler }); await settle(); cancelled.cancel();
  await cancelled.node("proxy-identity-form").emit("submit");
  assert.equal(cancelled.calls.length, 2);
  const p = page({ handler }); await settle();
  await p.node("proxy-identity-form").emit("submit");
  const write = p.calls.find((call) => call.url === "/admin/proxy/identities");
  assert.deepEqual(JSON.parse(JSON.stringify(write.body)), { name: "main", shop_id: SHOP });
  assert.equal(write.method, "POST");
  assert.equal(p.calls[2].url, "/auth/status");
  assert.equal(p.node("proxy-identity-name").value, "main");
  assert.doesNotMatch(JSON.stringify(write.body), /public_key|private_key|secret/);
});

test("main replacement fails closed after the owner session changes", async () => {
  const p = page({ handler(url) {
    if (url === "/auth/status") return { ...owner, session_id: "different-session" };
    if (url.includes("/identities")) return [currentIdentity];
    if (url.includes("/routes")) return [currentRoute];
  } });
  await settle(); await p.node("proxy-identity-form").emit("submit");
  assert.equal(p.calls.filter((call) => call.method === "POST").length, 0);
  assert.match(p.node("proxy-error").textContent, /登录身份已改变/);
});

test("historical identity deletion uses a scoped eligibility preview and fresh authorization", async () => {
  const p = page({ handler(url) {
    if (url === "/auth/status") return owner;
    if (url.endsWith("/cleanup-preview?shop_id=" + SHOP)) return { shop_id: SHOP, id: OLD_IDENTITY, eligible: true, route_count: 1 };
    if (url.includes("/identities") && url.includes("history=true")) return [currentIdentity, oldIdentity];
    if (url.includes("/routes")) return [currentRoute, oldRoute];
    return {};
  } });
  await settle(); await p.node("proxy-delete-identity-" + OLD_IDENTITY).emit("click");
  const write = p.calls.find((call) => call.method === "DELETE");
  assert.equal(write.url, "/admin/proxy/identities/" + OLD_IDENTITY + "?shop_id=" + SHOP);
  assert.equal(p.calls[2].url, "/admin/proxy/identities/" + OLD_IDENTITY + "/cleanup-preview?shop_id=" + SHOP);
  assert.equal(p.calls[3].url, "/auth/status");
  assert.equal(write.body, null);
});

test("referenced or foreign historical identity previews cannot trigger deletion", async () => {
  for (const preview of [
    { shop_id: SHOP, id: OLD_IDENTITY, eligible: false, route_count: 1 },
    { shop_id: "foreign-shop", id: OLD_IDENTITY, eligible: true, route_count: 1 },
    { shop_id: SHOP, id: IDENTITY, eligible: true, route_count: 1 },
  ]) {
    const p = page({ handler(url) {
      if (url.includes("cleanup-preview")) return preview;
      if (url.includes("/identities")) return [currentIdentity, oldIdentity];
      if (url.includes("/routes")) return [currentRoute, oldRoute];
    } });
    await settle(); await p.node("proxy-delete-identity-" + OLD_IDENTITY).emit("click");
    assert.equal(p.calls.filter((call) => call.method === "DELETE").length, 0);
    assert.equal(p.calls.filter((call) => call.url === "/auth/status").length, 0);
    assert.match(p.node("proxy-error").textContent, /不能清理|范围错误/);
  }
});

test("route cleanup confirms eligibility, supports cancellation, and retains referenced keys", async () => {
  const make = (eligible) => page({ handler(url) {
    if (url === "/auth/status") return owner;
    if (url.includes("cleanup-preview")) return { shop_id: SHOP, route_id: OLD_ROUTE, eligible, issued_card_count: eligible ? 0 : 2 };
    if (url.includes("/identities")) return [currentIdentity];
    if (url.includes("/routes") && url.includes("history=true")) return [currentRoute, oldRoute];
    return {};
  } });
  const referenced = make(false); await settle();
  await referenced.node("proxy-delete-route-" + OLD_ROUTE).emit("click");
  assert.equal(referenced.calls.filter((call) => call.method === "DELETE").length, 0);
  assert.match(referenced.node("proxy-error").textContent, /仍保护已发行卡密/);
  const cancelled = make(true); await settle(); cancelled.cancel();
  await cancelled.node("proxy-delete-route-" + OLD_ROUTE).emit("click");
  assert.equal(cancelled.calls.filter((call) => call.method === "DELETE").length, 0);
  const p = make(true); await settle(); await p.node("proxy-delete-route-" + OLD_ROUTE).emit("click");
  assert.equal(p.calls.find((call) => call.method === "DELETE").url, "/admin/proxy/routes/" + OLD_ROUTE + "?shop_id=" + SHOP);
  assert.equal(p.calls[3].url, "/auth/status");
});

test("bulk history cleanup stays scoped and reports only the server's actual deletion counts", async () => {
  const p = page({ handler(url, _body, method) {
    if (url === "/auth/status") return owner;
    if (url.startsWith("/admin/proxy/cleanup")) return method === "POST"
      ? { deleted_identity_count: 1, deleted_route_count: 2 }
      : { shop_id: SHOP, eligible_identity_count: 1, eligible_route_count: 2 };
    if (url.includes("/identities")) return [currentIdentity];
    if (url.includes("/routes")) return [currentRoute];
  } });
  await settle(); await p.node("proxy-cleanup-history").emit("click");
  const write = p.calls.find((call) => call.method === "POST");
  assert.equal(write.url, "/admin/proxy/cleanup");
  assert.deepEqual(JSON.parse(JSON.stringify(write.body)), { shop_id: SHOP });
  assert.equal(p.calls[2].url, "/admin/proxy/cleanup?shop_id=" + SHOP);
  assert.equal(p.calls[3].url, "/auth/status");
  assert.match(p.node("proxy-cleanup-status").textContent, /已清理 1 个标识与 2 条路由/);
});

test("empty or malformed bulk cleanup previews perform no mutation", async () => {
  for (const preview of [
    { shop_id: SHOP, eligible_identity_count: 0, eligible_route_count: 0 },
    { shop_id: "foreign-shop", eligible_identity_count: 1, eligible_route_count: 1 },
    { shop_id: SHOP, eligible_identity_count: "1", eligible_route_count: 1 },
  ]) {
    const p = page({ handler(url) {
      if (url.startsWith("/admin/proxy/cleanup")) return preview;
      if (url.includes("/identities")) return [currentIdentity];
      if (url.includes("/routes")) return [currentRoute];
    } });
    await settle(); await p.node("proxy-cleanup-history").emit("click");
    assert.equal(p.calls.filter((call) => call.method === "POST" || call.method === "DELETE").length, 0);
    assert.equal(p.calls.filter((call) => call.url === "/auth/status").length, 0);
    assert.match(p.node("proxy-cleanup-status").textContent + p.node("proxy-error").textContent, /没有可清理|清理预览无效/);
  }
});
