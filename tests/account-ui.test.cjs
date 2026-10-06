const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { appFixture } = require("./app-fixture.cjs");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/account.js"), "utf8");
const qrVendor = fs.readFileSync(path.join(__dirname, "../extore/static/vendor/qrcodegen.js"), "utf8");
const qrSource = fs.readFileSync(path.join(__dirname, "../extore/static/totp-qr.js"), "utf8");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const rootAuth = { role: "admin", superadmin: true, shop_id: null };
const shopAuth = { role: "admin", superadmin: false, shop_id: "shop-one" };
const setupSecret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"; // Synthetic enrollment only.
const setupUri = `otpauth://totp/Extore:owner%40example.test?secret=${setupSecret}&issuer=Extore&algorithm=SHA1&digits=6&period=30`;

function fixture(options = {}) {
  const nodes = new Map(), requests = [], navigations = [], copied = [], adopted = [], timers = new Map(), windowListeners = new Map();
  let current = true, passkeyCalls = 0, timerId = 0, now = Date.now();
  const decode = (text) => String(text).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  const element = (attributes = "", tagName = "div") => {
    const listeners = new Map(), children = new Set();
    const attributeValues = new Map([...attributes.matchAll(/\b([a-z][a-z0-9-]*)="([^"]*)"/gi)].map(([, name, value]) => [name, decode(value)]));
    const node = { attributes, value: attributeValues.get("value") || "", defaultValue: attributeValues.get("value") || "", checked: /\bchecked\b/.test(attributes), hidden: /(?:^|\s)hidden(?:\s|$)/.test(attributes), disabled: /(?:^|\s)disabled(?:\s|$)/.test(attributes), textContent: "", dataset: {}, isConnected: true,
      addEventListener(event, fn) { listeners.set(event, fn); }, emit(event) { if (tagName === "button" && node.disabled) return; return listeners.get(event)?.({ preventDefault() {} }); },
      setAttribute(name, value) { attributeValues.set(name, String(value)); }, getAttribute(name) { return attributeValues.get(name) ?? null; },
      focus() { node.focused = true; }, select() { node.selected = true; }, setSelectionRange(start, end) { node.selectionStart = start; node.selectionEnd = end; },
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => [...nodes.values()].filter((node) => selector === 'input[type="password"]' ? /\btype="password"/.test(node.attributes) : selector.startsWith("[data-") && node.attributes.includes(selector.slice(1, -1))),
    };
    for (const entry of attributes.matchAll(/\bdata-([a-z-]+)="([^"]*)"/g)) node.dataset[entry[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = decode(entry[2]);
    let markup = "";
    const disconnectChildren = () => {
      for (const [key, child] of children) {
        child.isConnected = false;
        child.disconnectChildren();
        if (nodes.get(key) === child) nodes.delete(key);
      }
      children.clear();
    };
    node.disconnectChildren = disconnectChildren;
    Object.defineProperty(node, "innerHTML", { get() { return markup; }, set(value) {
      markup = String(value);
      disconnectChildren();
      for (const tag of markup.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
        const attrs = tag[2], id = attrs.match(/\bid="([^"]+)"/)?.[1];
        if (!id && !attrs.includes("data-")) continue;
        const key = id ? "#" + id : "anonymous:" + nodes.size;
        const child = element(attrs, tag[1]);
        nodes.set(key, child); children.add([key, child]);
      }
    } });
    return node;
  };
  const root = element();
  class FixtureDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const sandbox = { AbortController, URL, URLSearchParams, Date: FixtureDate, console,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, due: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(event, listener) { if (!windowListeners.has(event)) windowListeners.set(event, new Set()); windowListeners.get(event).add(listener); },
    removeEventListener(event, listener) { windowListeners.get(event)?.delete(listener); },
    ExtoreClipboard: { async writeText(value) { copied.push(value); return options.clipboard ? options.clipboard(value) : true; } },
    fetch() { throw new Error("Account enrollment must not fetch a remote QR image"); },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(qrVendor, context);
  vm.runInContext(qrSource, context);
  if (options.qrUnavailable) context.ExtoreTotpQr = undefined;
  vm.runInContext(source, context);
  const ui = context.window.ExtoreAccount;
  const instance = ui.mount({ root, auth: options.auth || {}, mode: options.mode || "login", token: options.token || "", isCurrent: () => current,
    navigate: (url) => navigations.push(url), passkey: async () => { passkeyCalls++; return options.verified; }, onAuth: (auth) => adopted.push(auth),
    api(url, body, method, extra) { return new Promise((resolve, reject) => requests.push({ url, body, method, extra, resolve, reject })); },
  });
  return { root, ui, instance, requests, navigations, copied, adopted, timers, auth: options.auth || {}, node: (selector) => nodes.get(selector), leave() { current = false; },
    now: () => now,
    advance(ms, runTimers = true) {
      now += ms;
      if (!runTimers) return;
      for (const [id, timer] of [...timers]) if (timer.due <= now && timers.has(id)) { timers.delete(id); timer.callback(); }
    },
    emitWindow(event) { for (const listener of windowListeners.get(event) || []) listener(); },
    get passkeyCalls() { return passkeyCalls; },
  };
}

async function security(page, account = {}) {
  assert.equal(page.requests[0].url, "/auth/passkeys");
  page.requests[0].resolve([{ id: "key-one", name: '<img onerror="bad">', created: 1 }]);
  if (page.requests[1]) page.requests[1].resolve({ id: "shop-one", name: "Shop <one>", email: "owner@example.test", totp_enabled: false, ...account });
  await flush();
}

async function startSetup(page, result = {}) {
  page.node("#totp-password").value = "current-password";
  const offset = page.requests.length;
  const pending = page.node("#account-totp-form").emit("submit");
  assert.equal(page.requests[offset].url, "/auth/status");
  page.requests[offset].resolve(page.auth); await flush();
  assert.equal(page.requests[offset + 1].url, "/auth/totp/setup");
  page.requests[offset + 1].resolve({ secret: setupSecret, uri: setupUri, expires: page.now() / 1000 + 600, ...result });
  await flush();
  assert.equal(page.requests[offset + 2].url, "/auth/status");
  page.requests[offset + 2].resolve(page.auth);
  await pending;
}

async function authorize(page, auth = page.auth) {
  const request = page.requests.at(-1);
  assert.equal(request.url, "/auth/status");
  request.resolve(auth); await flush();
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

test("TOTP supports local QR and manual key copying, confirms a submitted code, and clears one-time recovery codes", async () => {
  const p = fixture({ auth: shopAuth, mode: "security" }); await security(p);
  await startSetup(p);
  assert.equal(p.node("#totp-password").value, "");
  assert.equal(p.node("#account-totp-form").hidden, true);
  assert.equal(p.node("#totp-qr-panel").hidden, false);
  assert.equal(p.node("#totp-manual-panel").hidden, true);
  assert.match(p.node("#totp-qr-code").innerHTML, /^<svg/);
  assert.doesNotMatch(p.node("#totp-qr-code").innerHTML, /otpauth|secret=|<img/);
  const key = p.node("#totp-secret"), uri = p.node("#totp-uri"), setup = p.node("#account-totp-setup");
  assert.equal(key.value, setupSecret);
  assert.equal(key.defaultValue, "");
  assert.equal(uri.value, setupUri);
  assert.equal(uri.defaultValue, "");
  assert.doesNotMatch(setup.innerHTML, new RegExp(setupSecret));
  assert.match(p.node("#totp-uri").attributes, /type="password"/);
  await p.node("#account-totp-manual").emit("click");
  assert.equal(p.node("#totp-qr-panel").hidden, true);
  assert.equal(p.node("#totp-manual-panel").hidden, false);
  assert.equal(p.node("#account-totp-manual").getAttribute("aria-pressed"), "true");
  const copying = p.node("#account-totp-copy-secret").emit("click");
  await authorize(p); await copying;
  assert.deepEqual(p.copied, [setupSecret]);
  assert.match(p.node("#totp-copy-status").textContent, /已复制/);
  const importCopy = p.node("#account-totp-copy").emit("click");
  await authorize(p); await importCopy;
  assert.deepEqual(p.copied, [setupSecret, setupUri]);
  p.node("#totp-confirm").value = "012345";
  const confirm = p.node("#account-totp-confirm-form").emit("submit");
  await authorize(p);
  assert.equal(p.requests.at(-1).url, "/auth/totp/confirm");
  assert.equal(p.requests.at(-1).body.code, "012345");
  p.requests.at(-1).resolve({ ok: true, backup_codes: ["ONE-RECOVERY-CODE", "TWO-RECOVERY-CODE"] }); await confirm;
  assert.match(p.root.innerHTML, /仅显示这一次/);
  assert.match(p.root.innerHTML, /ONE-RECOVERY-CODE/);
  assert.equal(key.value, ""); assert.equal(uri.value, "");
  assert.equal(setup.innerHTML, "");
  assert.equal(p.node("#totp-qr-code"), undefined);
  assert.equal(p.timers.size, 0);
  const closing = p.node("#account-recovery-close").emit("click");
  p.requests.at(-2).resolve([]); p.requests.at(-1).resolve({ id: "shop-one", name: "Shop", email: "owner@example.test", totp_enabled: true }); await closing;
  assert.doesNotMatch(p.root.innerHTML, /ONE-RECOVERY-CODE/);
});

test("TOTP manual key remains selectable when clipboard access is denied or throws", async () => {
  for (const clipboard of [() => false, () => { throw new Error("Clipboard denied"); }]) {
    const p = fixture({ auth: shopAuth, mode: "security", clipboard }); await security(p); await startSetup(p);
    await p.node("#account-totp-manual").emit("click");
    const key = p.node("#totp-secret");
    const copying = p.node("#account-totp-copy-secret").emit("click");
    await authorize(p); await copying;
    assert.deepEqual(p.copied, [setupSecret]);
    assert.equal(key.value, setupSecret);
    assert.equal(key.focused, true); assert.equal(key.selected, true);
    assert.equal(key.selectionStart, 0); assert.equal(key.selectionEnd, setupSecret.length);
    assert.match(p.node("#totp-copy-status").textContent, /复制失败/);
    assert.doesNotMatch(p.node("#totp-copy-status").textContent, /已复制/);
    p.instance.dispose();
  }
});

test("TOTP confirmation rejects malformed codes locally and preserves enrollment after a wrong valid code", async () => {
  const p = fixture({ auth: { ...shopAuth, session_id: "original-session" }, mode: "security" }); await security(p); await startSetup(p);
  const key = p.node("#totp-secret"), qr = p.node("#totp-qr-code").innerHTML, code = p.node("#totp-confirm");
  const count = p.requests.length;
  code.value = "123";
  await p.node("#account-totp-confirm-form").emit("submit");
  assert.equal(p.requests.length, count);
  assert.match(p.node("#account-error").textContent, /六位/);
  code.value = "012345";
  const first = p.node("#account-totp-confirm-form").emit("submit");
  assert.equal(p.node("#account-totp-cancel").disabled, true);
  await authorize(p);
  assert.equal(p.requests.at(-1).extra.expectedScope, "shop-one");
  assert.equal(p.requests.at(-1).extra.expectedSessionId, "original-session");
  p.requests.at(-1).reject(new Error("验证码错误")); await first;
  assert.equal(code.value, "");
  assert.equal(key.value, setupSecret);
  assert.equal(p.node("#totp-qr-code").innerHTML, qr);
  assert.equal(p.node("#account-totp-cancel").disabled, false);
  code.value = "023456";
  const retry = p.node("#account-totp-confirm-form").emit("submit");
  await authorize(p);
  assert.equal(p.requests.at(-1).body.code, "023456");
  p.requests.at(-1).resolve({ backup_codes: ["RETRY-RECOVERY-CODE"] }); await retry;
  assert.match(p.root.innerHTML, /RETRY-RECOVERY-CODE/);
  assert.equal(key.value, "");
});

test("TOTP expires and clears QR and keys even when a background timer has not run", async () => {
  for (const trigger of ["timer", "copy", "submit"]) {
    const p = fixture({ auth: shopAuth, mode: "security" }); await security(p); await startSetup(p);
    const key = p.node("#totp-secret"), uri = p.node("#totp-uri"), setup = p.node("#account-totp-setup"), form = p.node("#account-totp-form");
    const count = p.requests.length;
    p.advance(600001, trigger === "timer");
    if (trigger === "copy") await p.node("#account-totp-copy-secret").emit("click");
    if (trigger === "submit") { p.node("#totp-confirm").value = "012345"; await p.node("#account-totp-confirm-form").emit("submit"); }
    assert.equal(p.requests.length, count);
    assert.deepEqual(p.copied, []);
    assert.equal(key.value, ""); assert.equal(uri.value, "");
    assert.equal(setup.innerHTML, ""); assert.equal(form.hidden, false);
    assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(p.timers.size, 0);
    assert.match(p.node("#account-totp-notice").textContent, /已过期/);
  }
});

test("TOTP blocks copying and confirming after either an account or a session switch", async () => {
  const auth = { ...shopAuth, session_id: "original-session" };
  for (const changed of [{ ...auth, shop_id: "shop-two" }, { ...auth, session_id: "new-session" }]) {
    for (const action of ["copy", "confirm"]) {
      const p = fixture({ auth, mode: "security" }); await security(p); await startSetup(p);
      const key = p.node("#totp-secret"), setup = p.node("#account-totp-setup"), count = p.requests.length;
      p.node("#totp-confirm").value = "012345";
      const pending = p.node(action === "copy" ? "#account-totp-copy-secret" : "#account-totp-confirm-form").emit(action === "copy" ? "click" : "submit");
      await authorize(p, changed); await pending;
      assert.equal(p.requests.length, count + 1);
      assert.deepEqual(p.copied, []);
      assert.equal(key.value, ""); assert.equal(setup.innerHTML, "");
      assert.equal(p.node("#totp-qr-code"), undefined);
      assert.match(p.node("#account-error").textContent, /账户或店铺已改变/);
    }
  }
});

test("TOTP never exposes a late setup response after disposal or pagehide", async () => {
  for (const leave of ["dispose", "pagehide"]) {
    const p = fixture({ auth: shopAuth, mode: "security" }); await security(p);
    p.node("#totp-password").value = "current-password";
    const pending = p.node("#account-totp-form").emit("submit");
    await authorize(p);
    assert.equal(p.requests.at(-1).url, "/auth/totp/setup");
    const setupRequest = p.requests.at(-1), count = p.requests.length;
    if (leave === "dispose") p.instance.dispose(); else p.emitWindow("pagehide");
    const response = { secret: setupSecret, uri: setupUri, expires: p.now() / 1000 + 600 };
    setupRequest.resolve(response); await pending;
    assert.equal(p.requests.length, count);
    assert.equal(p.node("#totp-secret"), undefined);
    assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(p.node("#totp-password").value, "");
    assert.equal(response.secret, ""); assert.equal(response.uri, "");
    assert.equal(p.timers.size, 0);
  }
});

test("TOTP checks the response account and session before showing a newly generated key", async () => {
  const auth = { ...shopAuth, session_id: "original-session" };
  for (const changed of [{ ...auth, shop_id: "shop-two" }, { ...auth, session_id: "new-session" }]) {
    const p = fixture({ auth, mode: "security" }); await security(p);
    p.node("#totp-password").value = "current-password";
    const pending = p.node("#account-totp-form").emit("submit");
    await authorize(p);
    const response = { secret: setupSecret, uri: setupUri, expires: p.now() / 1000 + 600 };
    p.requests.at(-1).resolve(response); await flush();
    await authorize(p, changed); await pending;
    assert.equal(p.node("#totp-secret"), undefined);
    assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(response.secret, ""); assert.equal(response.uri, "");
    assert.match(p.node("#account-error").textContent, /账户或店铺已改变/);
  }
});

test("TOTP cancellation during clipboard work removes the key and ignores late copy feedback", async () => {
  let resolveCopy;
  const p = fixture({ auth: shopAuth, mode: "security", clipboard: () => new Promise((resolve) => { resolveCopy = resolve; }) }); await security(p); await startSetup(p);
  const key = p.node("#totp-secret"), uri = p.node("#totp-uri"), setup = p.node("#account-totp-setup"), status = p.node("#totp-copy-status");
  const copying = p.node("#account-totp-copy-secret").emit("click");
  await authorize(p);
  assert.equal(typeof resolveCopy, "function");
  await p.node("#account-totp-cancel").emit("click");
  assert.equal(key.value, ""); assert.equal(uri.value, "");
  assert.equal(setup.innerHTML, ""); assert.equal(p.node("#totp-qr-code"), undefined);
  assert.equal(p.timers.size, 0);
  resolveCopy(true); await copying;
  assert.doesNotMatch(status.textContent, /已复制/);
  assert.equal(p.node("#totp-copy-status"), undefined);
  assert.match(p.node("#account-totp-notice").textContent, /尚未开启/);
});

test("TOTP stays available while switching to an authenticator app but clears on pagehide and disposal", async () => {
  for (const leave of ["pagehide", "dispose"]) {
    const p = fixture({ auth: shopAuth, mode: "security" }); await security(p); await startSetup(p);
    const key = p.node("#totp-secret"), uri = p.node("#totp-uri"), code = p.node("#totp-confirm"), setup = p.node("#account-totp-setup");
    code.value = "012345";
    p.emitWindow("visibilitychange");
    assert.equal(key.value, setupSecret);
    assert.match(p.node("#totp-qr-code").innerHTML, /^<svg/);
    if (leave === "dispose") p.instance.dispose(); else p.emitWindow("pagehide");
    assert.equal(key.value, ""); assert.equal(uri.value, ""); assert.equal(code.value, "");
    assert.equal(key.defaultValue, ""); assert.equal(uri.defaultValue, "");
    assert.equal(setup.innerHTML, ""); assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(p.timers.size, 0);
  }
});

test("a late TOTP confirmation cannot show recovery codes after pagehide or a replacement account mount", async () => {
  for (const leave of ["pagehide", "remount"]) {
    const p = fixture({ auth: shopAuth, mode: "security" }); await security(p); await startSetup(p);
    const key = p.node("#totp-secret"), setup = p.node("#account-totp-setup");
    p.node("#totp-confirm").value = "012345";
    const pending = p.node("#account-totp-confirm-form").emit("submit");
    await authorize(p);
    const confirmation = p.requests.at(-1);
    assert.equal(confirmation.url, "/auth/totp/confirm");
    if (leave === "pagehide") p.emitWindow("pagehide");
    else p.ui.mount({ root: p.root, auth: { ...shopAuth, shop_id: "shop-two" }, mode: "confirm", api() { throw new Error("Unexpected request from replacement mount"); } });
    const markup = p.root.innerHTML;
    confirmation.resolve({ backup_codes: ["LATE-RECOVERY-CODE"] }); await pending;
    assert.equal(p.root.innerHTML, markup);
    assert.doesNotMatch(p.root.innerHTML, /LATE-RECOVERY-CODE/);
    assert.equal(key.value, ""); assert.equal(setup.innerHTML, "");
    assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(p.timers.size, 0);
  }
});

test("TOTP rejects mismatched or expired enrollment data and falls back when the QR encoder is unavailable", async () => {
  for (const result of [{ secret: "invalid-secret" }, { uri: setupUri.replace(setupSecret, "A".repeat(32)) }, { expires: 1 }]) {
    const p = fixture({ auth: shopAuth, mode: "security" }); await security(p); await startSetup(p, result);
    assert.equal(p.node("#totp-secret"), undefined);
    assert.equal(p.node("#totp-qr-code"), undefined);
    assert.equal(p.timers.size, 0);
    assert.match(p.node("#account-error").textContent, /无效或已过期/);
  }
  const p = fixture({ auth: shopAuth, mode: "security", qrUnavailable: true }); await security(p); await startSetup(p);
  assert.equal(p.node("#totp-qr-panel").hidden, true);
  assert.equal(p.node("#totp-manual-panel").hidden, false);
  assert.equal(p.node("#account-totp-qr").disabled, true);
  assert.equal(p.node("#totp-secret").value, setupSecret);
  const copying = p.node("#account-totp-copy-secret").emit("click");
  await authorize(p); await copying;
  assert.deepEqual(p.copied, [setupSecret]);
  p.instance.dispose();
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
