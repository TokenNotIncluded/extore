const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/proxy-config.js"), "utf8");
const SHOP = "12345678-1234-1234-1234-123456789012";
const ROUTE = "a".repeat(32), IDENTITY = "b".repeat(32);
const publicRoute = { route_id: ROUTE, issuer_id: IDENTITY, name: "Issuer <safe>", origin: "https://issuer.example", path: "/", public_key: "A".repeat(43) };
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
