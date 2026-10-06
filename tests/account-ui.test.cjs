const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { appFixture } = require("./app-fixture.cjs");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/account.js"), "utf8");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const rootAuth = { role: "admin", superadmin: true, shop_id: null };
const shopAuth = { role: "admin", superadmin: false, shop_id: "shop-one" };

function fixture(options = {}) {
  const nodes = new Map(), requests = [], navigations = [], copied = [], adopted = [];
  let current = true, passkeyCalls = 0;
  const decode = (text) => String(text).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  const element = (attributes = "") => {
    const listeners = new Map(), children = new Set();
    const node = { attributes, value: decode(attributes.match(/\bvalue="([^"]*)"/)?.[1] || ""), checked: /\bchecked\b/.test(attributes), disabled: false, textContent: "", dataset: {}, isConnected: true,
      addEventListener(event, fn) { listeners.set(event, fn); }, emit(event) { return listeners.get(event)?.({ preventDefault() {} }); },
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => [...nodes.values()].filter((node) => selector === 'input[type="password"]' ? /\btype="password"/.test(node.attributes) : selector.startsWith("[data-") && node.attributes.includes(selector.slice(1, -1))),
    };
    for (const entry of attributes.matchAll(/\bdata-([a-z-]+)="([^"]*)"/g)) node.dataset[entry[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = decode(entry[2]);
    let markup = "";
    Object.defineProperty(node, "innerHTML", { get() { return markup; }, set(value) {
      markup = String(value);
      for (const child of children) { nodes.get(child) && (nodes.get(child).isConnected = false); nodes.delete(child); }
      children.clear();
      for (const tag of markup.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
        const attrs = tag[2], id = attrs.match(/\bid="([^"]+)"/)?.[1];
        if (!id && !attrs.includes("data-")) continue;
        const key = id ? "#" + id : "anonymous:" + nodes.size;
        nodes.set(key, element(attrs)); children.add(key);
      }
    } });
    return node;
  };
  const root = element();
  const context = vm.createContext({ window: { ExtoreClipboard: { async writeText(value) { copied.push(value); return true; } } }, AbortController, URLSearchParams, console });
  vm.runInContext(source, context);
  const ui = context.window.ExtoreAccount;
  const instance = ui.mount({ root, auth: options.auth || {}, mode: options.mode || "login", token: options.token || "", isCurrent: () => current,
    navigate: (url) => navigations.push(url), passkey: async () => { passkeyCalls++; return options.verified; }, onAuth: (auth) => adopted.push(auth),
    api(url, body, method, extra) { return new Promise((resolve, reject) => requests.push({ url, body, method, extra, resolve, reject })); },
  });
  return { root, ui, instance, requests, navigations, copied, adopted, node: (selector) => nodes.get(selector), leave() { current = false; }, get passkeyCalls() { return passkeyCalls; } };
}

async function security(page, account = {}) {
  assert.equal(page.requests[0].url, "/auth/passkeys");
  page.requests[0].resolve([{ id: "key-one", name: '<img onerror="bad">', created: 1 }]);
  if (page.requests[1]) page.requests[1].resolve({ id: "shop-one", name: "Shop <one>", email: "owner@example.test", totp_enabled: false, ...account });
  await flush();
}

test("superadmin controls require explicit root status and merchants remain scoped", () => {
  const p = fixture();
  assert.equal(p.ui.rootScope(rootAuth), true);
  for (const value of [{ role: "admin" }, { role: "admin", superadmin: false, shop_id: null }, { ...rootAuth, shop_id: "shop-one" }, { ...rootAuth, role: "staff" }]) assert.equal(p.ui.rootScope(value), false);
  assert.equal(p.ui.shopScope(shopAuth), true);
  assert.equal(p.ui.sameScope(shopAuth, { ...shopAuth, shop_id: "shop-two" }), false);
  assert.equal(p.ui.sameScope(rootAuth, shopAuth), false);
});

test("account routes support fresh fragments and old email links without exposing tokens", () => {
  const token = "safe-token-12345678901234567890";
  const p = fixture({ mode: "invite", token });
  assert.equal(p.ui.route("/account/invite", "#" + token).token, token);
  assert.equal(p.ui.route("/account", "#reset=" + token).mode, "reset");
  assert.doesNotMatch(p.root.innerHTML, new RegExp(token));
  assert.match(p.root.innerHTML, /type="password"/);
  assert.doesNotMatch(fixture().root.innerHTML, /href="\/account\/register"/);
  assert.match(fixture({ auth: { registration_enabled: true } }).root.innerHTML, /href="\/account\/register"/);
});

test("email login supplies fresh factors and clears secret inputs after a failed request", async () => {
  const p = fixture();
  p.node("#login-email").value = "owner@example.test";
  p.node("#login-password").value = "private-password";
  p.node("#login-code").value = "123456";
  const pending = p.node("#account-login-form").emit("submit");
  assert.equal(p.requests[0].url, "/auth/email/login");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body)), { email: "owner@example.test", password: "private-password", code: "123456" });
  p.requests[0].reject(new Error("Invalid credentials"));
  await pending;
  assert.equal(p.node("#login-password").value, "");
  assert.equal(p.node("#login-code").value, "");
  assert.equal(p.node("#account-error").textContent, "Invalid credentials");
});

test("late login and account reads cannot navigate or render after disposal", async () => {
  const p = fixture();
  p.node("#login-email").value = "owner@example.test";
  p.node("#login-password").value = "secret";
  const pending = p.node("#account-login-form").emit("submit");
  p.instance.dispose();
  p.root.innerHTML = "New page";
  p.requests[0].resolve({ role: "admin" });
  await pending;
  assert.deepEqual(p.navigations, []);
  assert.equal(p.root.innerHTML, "New page");
  const s = fixture({ auth: shopAuth, mode: "security" });
  s.leave(); s.root.innerHTML = "Changed route";
  await security(s);
  assert.equal(s.root.innerHTML, "Changed route");
});

test("root shop creation waits for fresh authentication and rejects a switched Passkey account", async () => {
  const p = fixture({ auth: rootAuth, mode: "shops" });
  p.requests[0].resolve([{ id: "shop-one", name: "Shop <one>", email: "owner@example.test", enabled: true, verified: true }]); await flush();
  assert.match(p.root.innerHTML, /Shop &lt;one&gt;/);
  p.node("#shop-name").value = "New shop"; p.node("#shop-email").value = "new@example.test";
  await p.node("#account-shop-form").emit("submit");
  assert.equal(p.requests.length, 1);
  assert.equal(p.node("#account-fresh-form"), undefined);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  assert.equal(p.requests[1].url, "/auth/status");
  p.requests[1].resolve(shopAuth); await pending;
  assert.equal(p.passkeyCalls, 1);
  assert.equal(p.requests.length, 2);
  assert.match(p.node("#account-error").textContent, /账户或店铺已改变/);
});

test("root SMTP updates preserve omitted credentials and remain behind fresh verification", async () => {
  const p = fixture({ auth: rootAuth, mode: "mail" });
  p.requests[0].resolve({ registration_enabled: false, smtp: { enabled: true, host: "smtp.example.test", port: 587, mode: "starttls", sender: "sender@example.test", has_username: true, has_password: true } }); await flush();
  assert.equal(p.node("#mail-password").value, "");
  assert.equal(p.node("#mail-user").value, "");
  await p.node("#account-mail-form").emit("submit");
  assert.equal(p.requests.length, 1);
  const pending = p.node("#account-fresh-passkey").emit("click"); await flush();
  p.requests[1].resolve(rootAuth); await flush();
  assert.equal(p.requests[2].url, "/platform/settings");
  assert.equal(p.requests[2].method, "PUT");
  assert.equal(Object.hasOwn(p.requests[2].body.smtp, "password"), false);
  assert.equal(Object.hasOwn(p.requests[2].body.smtp, "username"), false);
  p.requests[2].reject(new Error("save rejected")); await pending;
});

test("merchant security escapes metadata and refuses a different shop account", async () => {
  const p = fixture({ auth: shopAuth, mode: "security" }); await security(p);
  assert.match(p.root.innerHTML, /Shop &lt;one&gt;/);
  assert.match(p.root.innerHTML, /&lt;img onerror=&quot;bad&quot;&gt;/);
  assert.doesNotMatch(p.root.innerHTML, /<img onerror/);
  const q = fixture({ auth: shopAuth, mode: "security" }); await security(q, { id: "shop-two" });
  assert.match(q.node("#account-error").textContent, /范围不一致/);
  assert.doesNotMatch(q.root.innerHTML, /owner@example/);
});

test("TOTP setup stays local, confirms fresh code, and recovery codes disappear on close", async () => {
  const p = fixture({ auth: shopAuth, mode: "security" }); await security(p);
  p.node("#totp-password").value = "current-password";
  const pending = p.node("#account-totp-form").emit("submit");
  p.requests[2].resolve(shopAuth); await flush();
  assert.equal(p.requests[3].url, "/auth/totp/setup");
  p.requests[3].resolve({ secret: "private-totp-secret", uri: "otpauth://totp/shop?secret=private-totp-secret", expires: Date.now() / 1000 + 600 }); await pending;
  assert.equal(p.node("#totp-password").value, "");
  assert.match(p.node("#totp-secret").attributes, /type="password"/);
  assert.match(p.node("#totp-uri").attributes, /type="password"/);
  assert.doesNotMatch(p.root.innerHTML, /<img|https?:\/\/.*private-totp-secret/);
  await p.node("#account-totp-copy").emit("click");
  assert.deepEqual(p.copied, ["otpauth://totp/shop?secret=private-totp-secret"]);
  p.node("#totp-confirm").value = "456789";
  const confirm = p.node("#account-totp-confirm").emit("click");
  p.requests[4].resolve({ ok: true, backup_codes: ["ONE-RECOVERY-CODE", "TWO-RECOVERY-CODE"] }); await confirm;
  assert.match(p.root.innerHTML, /仅显示这一次/);
  assert.match(p.root.innerHTML, /ONE-RECOVERY-CODE/);
  const closing = p.node("#account-recovery-close").emit("click");
  p.requests[5].resolve([]); p.requests[6].resolve({ id: "shop-one", name: "Shop", email: "owner@example.test", totp_enabled: true }); await closing;
  assert.doesNotMatch(p.root.innerHTML, /ONE-RECOVERY-CODE|private-totp-secret/);
});

test("profile updates are metadata-only and omit every unfilled secret", async () => {
  const p = fixture({ auth: shopAuth, mode: "profiles" });
  p.requests[0].resolve([{ id: "processor", name: "Processor", shop_configuration: [{ key: "account", label: "付款账号", required: true }, { key: "token", label: "密钥", required: true }] }]); await flush();
  p.requests[1].resolve([{ id: "profile-one", shop_id: "shop-one", processor_id: "processor", name: "Account <one>", revision: 2 }]); await flush();
  assert.match(p.root.innerHTML, /Account &lt;one&gt;/);
  await p.root.querySelectorAll("[data-profile-edit]")[0].emit("click");
  assert.equal(p.node("#profile-update-profile-one-0").value, "");
  assert.match(p.node("#profile-update-profile-one-0").attributes, /type="password"/);
  p.node("#profile-update-profile-one-1").value = "replacement-token";
  await p.node("#profile-update-profile-one").emit("submit");
  assert.equal(p.requests.length, 2);
  p.node("#fresh-password").value = "current-password";
  const pending = p.node("#account-fresh-form").emit("submit");
  p.requests[2].resolve({ ok: true }); await flush(); p.requests[3].resolve(shopAuth); await flush();
  assert.equal(p.requests[4].url, "/admin/processor-profiles/profile-one");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[4].body.configuration)), { token: "replacement-token" });
  p.requests[4].reject(new Error("rejected")); await pending;
  assert.equal(Object.hasOwn(p.requests[4].body, "configuration"), false);
});

test("root profile reads explicitly name their target shop and staff never get a profiles page", async () => {
  const p = fixture({ auth: rootAuth, mode: "profiles" });
  p.requests[0].resolve([]); p.requests[1].resolve([{ id: "shop-one", name: "Shop" }]); await flush();
  assert.equal(p.requests[2].url, "/admin/processor-profiles?shop_id=shop-one");
  const q = fixture({ auth: { role: "staff", shop_id: "shop-one" }, mode: "profiles" }); await flush();
  assert.equal(q.requests.length, 0);
  assert.match(q.node("#account-error").textContent, /仅店主/);
});

test("navigation exposes platform pages only to root and header context is a native home link", () => {
  const p = appFixture();
  p.context.window.ExtoreAccount = fixture().ui;
  p.context.acceptAuth(shopAuth); assert.equal(p.context.managementTabs().some(([key]) => key === "shops"), false);
  p.context.acceptAuth(rootAuth); assert.equal(p.context.managementTabs().some(([key]) => key === "shops"), true);
  p.context.acceptAuth({ role: "admin" }); assert.equal(p.context.managementTabs().some(([key]) => key === "mail"), false);
  const html = fs.readFileSync(path.join(__dirname, "../extore/static/index.html"), "utf8");
  assert.match(html, /<a id="header-context"[^>]*href="\/"/);
});

test("a name change refuses a replaced session and sends the original session binding", async () => {
  const auth = { ...shopAuth, session_id: "old-session" };
  const p = fixture({ auth, mode: "security" }); await security(p);
  p.node("#account-name").value = "New name";
  const pending = p.node("#account-name-form").emit("submit");
  p.requests[2].resolve({ ...auth, session_id: "other-session" }); await pending;
  assert.equal(p.requests.length, 3);
  assert.match(p.node("#account-error").textContent, /账户或店铺已改变/);
  assert.equal(p.ui.sameScope(auth, { ...auth, session_id: "other-session" }), false);
  const q = fixture({ auth, mode: "security" }); await security(q);
  q.node("#account-name").value = "New name";
  const saving = q.node("#account-name-form").emit("submit");
  q.requests[2].resolve(auth); await flush();
  assert.equal(q.requests[3].extra.expectedScope, "shop-one");
  assert.equal(q.requests[3].extra.expectedSessionId, "old-session");
  q.requests[3].reject(new Error("stop")); await saving;
});

test("password change adopts only a new session for the verified original shop", async () => {
  const auth = { ...shopAuth, session_id: "old-session" };
  const p = fixture({ auth, mode: "security" }); await security(p);
  p.node("#change-password").value = "old-private-password";
  p.node("#change-new").value = "new-private-password";
  const pending = p.node("#account-password-form").emit("submit");
  p.requests[2].resolve(auth); await flush();
  assert.equal(p.requests[3].extra.expectedSessionId, "old-session");
  p.requests[3].resolve({ ok: true }); await flush();
  assert.equal(p.requests[4].url, "/auth/status");
  p.requests[4].resolve({ ...auth, session_id: "new-session" }); await flush();
  assert.equal(p.adopted[0].session_id, "new-session");
  assert.equal(p.requests[5].extra.expectedSessionId, "new-session");
  p.requests[5].resolve([]); p.requests[6].resolve({ id: "shop-one", name: "Shop", email: "owner@example.test" }); await pending;
  assert.doesNotMatch(p.root.innerHTML, /old-private-password|new-private-password/);
});

test("fresh Passkey may replace the session but cannot change the target shop", async () => {
  const auth = { ...shopAuth, session_id: "old-session" };
  const verified = { ...auth, session_id: "new-session" };
  const p = fixture({ auth, verified, mode: "confirm" });
  let called = 0;
  p.instance.confirmFresh(async () => { called++; });
  await p.node("#account-fresh-passkey").emit("click");
  assert.equal(called, 1); assert.equal(p.adopted[0].session_id, "new-session");
  const q = fixture({ auth, verified: { ...verified, shop_id: "shop-two" }, mode: "confirm" });
  q.instance.confirmFresh(async () => { called++; });
  await q.node("#account-fresh-passkey").emit("click");
  assert.equal(called, 1); assert.equal(q.adopted.length, 0);
  assert.match(q.node("#account-error").textContent, /账户或店铺已改变/);
});
