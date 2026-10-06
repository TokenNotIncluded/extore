"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const { webcrypto, generateKeyPairSync, sign, createHash, randomUUID } = require("node:crypto");
const { appFixture, flush } = require("./app-fixture.cjs");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/proxy-routing.js"), "utf8");
const hexID = () => randomUUID().replace(/-/g, "");
const plain = (value) => JSON.parse(JSON.stringify(value));

function issuer(origin = "https://issuer.example.com", name = "文档领取站") {
  const keys = generateKeyPairSync("ed25519");
  const publicBytes = keys.publicKey.export({ format: "der", type: "spki" }).subarray(-32);
  const route = { route_id: hexID(), issuer_id: hexID(), public_key: publicBytes.toString("base64url"), origin, path: "/", name };
  function code(secret = "A".repeat(32)) {
    const digest = createHash("sha256").update(secret).digest("hex");
    const message = Buffer.from(`Extore routed code v1\n${route.route_id}\n${route.issuer_id}\n${route.origin}\n${route.path}\n${digest}`);
    return `EXR1.${route.route_id}.${secret}.${route.issuer_id}.${sign(null, message, keys.privateKey).toString("base64url")}`;
  }
  return { route, code };
}
function browser({ hash = "", pathname = "/", metadata = [], crypto = webcrypto } = {}) {
  const requests = [], navigations = [], replacements = [];
  const location = { origin: "https://proxy.example.com", pathname, hash, search: "", replace(url) { navigations.push(url); } };
  const context = vm.createContext({ URL, TextEncoder, Uint8Array, atob, btoa, crypto, location,
    history: { replaceState(_state, _title, url) { replacements.push(url); location.hash = ""; } },
    fetch(url, options) { requests.push({ url, options }); return Promise.resolve({ ok: true, json: async () => metadata }); },
    localStorage: new Proxy({}, { get() { throw new Error("Never touch card storage"); } }),
    sessionStorage: new Proxy({}, { get() { throw new Error("Never touch card storage"); } }),
    console: new Proxy({}, { get() { throw new Error("Never log cards"); } }),
  });
  vm.runInContext(source, context);
  return { context, api: context.ExtoreProxyRouting, location, requests, navigations, replacements };
}
function pane(groups, localCodes = []) {
  const nodes = new Map();
  const node = (key) => {
    if (!nodes.has(key)) nodes.set(key, {
      dataset: { proxyRoute: key.split(":")[1] }, disabled: false, textContent: "", events: {},
      addEventListener(type, fn) { this.events[type] = fn; },
      setAttribute() {}, focus() { this.focused = true; },
      async click() { return this.events.click?.({ currentTarget: this }); },
    });
    return nodes.get(key);
  };
  return { innerHTML: "", querySelectorAll(selector) { assert.equal(selector, "[data-proxy-route]"); return groups.map((_, i) => node("route:" + i)); },
    querySelector(selector) { if (selector === "#proxy-local" && !localCodes.length) return null; return node(selector); }, node,
  };
}

test("legacy codes never request metadata or probe an issuer", async () => {
  const page = browser();
  const raw = "aaaaaaaa bbbbbbbb cccccccc dddddddd";
  assert.deepEqual(plain(await page.api.routeCodes(raw)), { localCodes: [raw], groups: [] });
  assert.equal(page.requests.length, 0);
  assert.equal(page.navigations.length, 0);
});

test("all signatures verify locally; A only receives a fixed metadata GET", async () => {
  const a = issuer(), b = issuer("https://another.example.com");
  const page = browser({ metadata: [a.route, b.route] });
  const code = a.code(), another = b.code("B".repeat(32));
  const result = await page.api.routeCodes(code + "\n" + another + "\n" + code);
  assert.equal(result.groups.length, 2);
  assert.equal(result.groups[0].codes.length, 1);
  assert.equal(page.navigations.length, 0);
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/proxy/routes");
  assert.equal(page.requests[0].options.body, undefined);
  assert.equal(page.requests[0].options.credentials, "omit");
  assert.equal(page.requests[0].options.redirect, "error");
  assert.ok(!JSON.stringify(page.requests).includes(code));
  assert.ok(!JSON.stringify(page.requests).includes("A".repeat(32)));
  assert.ok(!JSON.stringify(page.api.publicResult(result)).includes(code));
});

for (const part of [0, 1, 2, 3, 4]) test(`tampered wrapper part ${part} rejects before navigation, even after a valid duplicate`, async () => {
  const current = issuer();
  const page = browser({ metadata: [current.route] });
  const code = current.code();
  const pieces = code.split(".");
  pieces[part] = part === 0 ? "EXR2" : (part === 2 ? "B" : part === 4 ? (pieces[4][0] === "B" ? "A" : "B") : "f") + pieces[part].slice(1);
  if (pieces.join(".") === code) pieces[part] = "0" + pieces[part].slice(1);
  await assert.rejects(page.api.routeCodes(code + "\n" + pieces.join(".")), /Unable to verify/);
  assert.equal(page.navigations.length, 0);
  assert.equal(page.requests.length, 1);
});

test("unknown route, duplicate pins, changed key, origin, path and issuer fail closed", async () => {
  const current = issuer(), other = issuer();
  for (const metadata of [[], [current.route, current.route], [{ ...current.route, public_key: other.route.public_key }], [{ ...current.route, origin: "https://changed.example.com" }], [{ ...current.route, path: "/changed" }], [{ ...current.route, issuer_id: other.route.issuer_id }]]) {
    const page = browser({ metadata });
    await assert.rejects(page.api.routeCodes(current.code()), /Unable to verify/);
    assert.equal(page.navigations.length, 0);
  }
});

test("unavailable Ed25519 never falls back to server validation", async () => {
  const current = issuer();
  const page = browser({ metadata: [current.route], crypto: { subtle: { digest: webcrypto.subtle.digest.bind(webcrypto.subtle), importKey: async () => { throw new Error("unsupported"); } } } });
  await assert.rejects(page.api.routeCodes(current.code()), /Unable to verify/);
  assert.equal(page.requests.length, 1);
  assert.equal(page.navigations.length, 0);
});

test("metadata accepts only canonical HTTPS domain and fixed path", () => {
  const current = issuer();
  const page = browser();
  for (const changes of [{ origin: "http://public.example.com" }, { origin: "https://localhost" }, { origin: "https://127.0.0.1" }, { origin: "https://8.8.8.8" }, { origin: "https://public.example.com/" }, { origin: "https://user@public.example.com" }, { origin: "https://public.example.com?query" }, { origin: "https://public.example.com#code" }, { origin: "https://-bad.example.com" }, { path: "//other" }, { path: "/a/../b" }, { path: "/%2e" }]) {
    assert.throws(() => page.api.safeRoute({ ...current.route, ...changes }), /Unable to verify/);
  }
});

test("fragment is cleared synchronously before decoding and consumed only once", () => {
  const current = issuer();
  const code = current.code();
  const page = browser({ hash: "#extore-code=" + encodeURIComponent(code) });
  assert.deepEqual(page.replacements, ["/"]);
  assert.equal(page.location.hash, "");
  assert.equal(page.requests.length, 0);
  assert.equal(page.api.consumeIncoming(), code);
  assert.equal(page.api.consumeIncoming(), "");
  const broken = browser({ hash: "#extore-code=%ZZ" });
  assert.deepEqual(broken.replacements, ["/"]);
  assert.throws(() => broken.api.consumeIncoming(), /Unable to verify/);
  assert.equal(broken.api.consumeIncoming(), "");
  const staff = browser({ pathname: "/staff", hash: "#extore-code=" + encodeURIComponent(code) });
  assert.deepEqual(staff.replacements, ["/staff"]);
  assert.equal(staff.api.consumeIncoming(), "");
});

test("group page shows counts and fixed destination, never raw cards; explicit click uses fragment only", async () => {
  const current = issuer("https://issuer.example.com", '<img src=x onerror="x">');
  const page = browser({ metadata: [current.route] });
  const code = current.code();
  const result = await page.api.routeCodes(code + "\nlegacy-card");
  const content = pane(result.groups, result.localCodes);
  let local;
  const response = page.api.renderRoutingChoice(content, result, { onLocal: (codes) => { local = codes; } });
  assert.equal(page.navigations.length, 0);
  assert.ok(content.innerHTML.includes("https://issuer.example.com"));
  assert.ok(content.innerHTML.includes("&lt;img"));
  assert.ok(!content.innerHTML.includes(code));
  assert.ok(!JSON.stringify(response).includes(code));
  assert.ok(!JSON.stringify(response).includes("legacy-card"));
  assert.equal(content.node("h1").focused, true);
  await content.node("#proxy-local").click();
  assert.equal(local, "legacy-card");
  await content.node("route:0").click();
  assert.equal(page.navigations.length, 1);
  const url = new URL(page.navigations[0]);
  assert.equal(url.origin, current.route.origin);
  assert.equal(url.pathname, "/");
  assert.equal(url.search, "");
  assert.equal(decodeURIComponent(url.hash.slice(13)), code);
  assert.ok(!url.pathname.includes(code));
});

test("local continue failure retains usable control and gives feedback", async () => {
  const current = issuer();
  const page = browser({ metadata: [current.route] });
  const result = await page.api.routeCodes(current.code() + "\nlegacy");
  const content = pane(result.groups, result.localCodes);
  page.api.renderRoutingChoice(content, result, { onLocal: async () => { throw new Error("请检查本地卡密"); } });
  await content.node("#proxy-local").click();
  assert.equal(content.node("#proxy-local").disabled, false);
  assert.equal(content.node("#proxy-error").textContent, "请检查本地卡密");
});

test("same-origin issuer remains local and does not navigate", async () => {
  const current = issuer("https://proxy.example.com");
  const page = browser({ metadata: [current.route] });
  const result = await page.api.routeCodes(current.code());
  assert.equal(result.groups.length, 0);
  assert.equal(result.localCodes.length, 1);
  assert.equal(page.navigations.length, 0);
});

test("exchange hook never posts a remote wrapper to A, and guards stale async routing", async () => {
  const current = issuer();
  const code = current.code();
  const page = appFixture();
  page.navigate("/");
  let finish;
  let renders = 0;
  page.context.window.ExtoreProxyRouting = {
    isRoutedCode: (value) => /^EXR[0-9]+(?:\.|$)/i.test(value),
    routeCodes: () => new Promise((resolve) => { finish = resolve; }),
    publicResult: (result) => ({ routing: true, groups: result.groups.map((group) => ({ origin: group.route.origin, count: group.codes.length })), local_count: result.localCodes.length }),
    renderRoutingChoice(_app, result) { renders++; return this.publicResult(result); },
  };
  const result = { groups: [{ route: current.route, codes: [code] }], localCodes: [] };
  const pending = page.context.exchangeCode(code);
  assert.equal(page.requests.length, 0);
  finish(result);
  const safe = await pending;
  assert.equal(renders, 1);
  assert.equal(page.requests.length, 0);
  assert.ok(!JSON.stringify(safe).includes(code));
  const stale = page.context.exchangeCode(code);
  page.navigate("/admin");
  finish(result);
  await stale;
  assert.equal(renders, 1);
  assert.equal(page.requests.length, 0);
  await flush();
});

test("module is loaded synchronously before any page/agent scripts", () => {
  const html = fs.readFileSync(path.join(__dirname, "../extore/static/index.html"), "utf8");
  const position = html.indexOf("/static/proxy-routing.js");
  assert.ok(position >= 0 && position < html.indexOf("/static/preferences.js"));
  assert.ok(position < html.indexOf("/static/webmcp.js"));
  assert.ok(position < html.indexOf("/static/app.js"));
  assert.match(html, /<script src="\/static\/proxy-routing\.js[^\"]*"><\/script>/);
});

test("a damaged wrapper separator cannot fall through to A's legacy POST", async () => {
  const current = issuer();
  const page = browser({ metadata: [current.route] });
  for (const separator of ["X", ":", "-", "/", "%2E"]) {
    const damaged = current.code().replace("EXR1.", "EXR1" + separator);
    assert.equal(page.api.isRoutedCode(damaged), true);
    await assert.rejects(page.api.routeCodes(damaged), /Unable to verify/);
  }
  const legacy = "EXR2ABCD-AAAAAAAA-AAAAAAAA-AAAAAAAA";
  assert.deepEqual(plain(await page.api.routeCodes(legacy)), { localCodes: [legacy], groups: [] });
});
