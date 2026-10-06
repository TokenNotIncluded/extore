const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/owner-cli.js"), "utf8");
const requestId = "36fc7bd8-a3e2-4f33-854b-7fe8c35d970c";
const now = 1791244800;
const bytes = (value) => Uint8Array.from(value).buffer;
const plain = (value) => JSON.parse(JSON.stringify(value));
const response = (overrides = {}) => ({
  request_id: requestId, device_code: "ABCD-EFGH-JKLM", client_name: "AI workstation",
  fingerprint: "a".repeat(64), role: "admin", scope: "shop.owner",
  grant_expires: now + 30 * 86400, expires: now + 300,
  options: { challenge: "AQID", rpId: "example.test", userVerification: "required", timeout: 60000,
    allowCredentials: [{ type: "public-key", id: "BAUG", transports: ["internal"] }] },
  ...overrides,
});
const passkeyCredential = () => ({
  id: "BAUG", type: "public-key", rawId: bytes([4, 5, 6]),
  response: { clientDataJSON: bytes([7, 8, 9]), authenticatorData: bytes([10, 11, 12]),
    signature: bytes([13, 14, 15]), userHandle: bytes([16, 17, 18]) },
  getClientExtensionResults: () => ({ appid: false }),
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function fixture(options = {}) {
  const calls = [], credentials = [], renderings = [], nodes = new Map();
  let html = "", clock = now * 1000;
  const root = {
    isConnected: true,
    get innerHTML() { return html; },
    set innerHTML(value) {
      for (const node of nodes.values()) node.isConnected = false;
      nodes.clear();
      html = value;
      renderings.push(value);
      for (const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
        const listeners = new Map();
        const node = { isConnected: true, disabled: false, textContent: "", value: match[2].match(/\bvalue="([^"]*)"/)?.[1] || "",
          addEventListener(event, handler) { listeners.set(event, handler); },
          focus() { this.focused = true; },
          emit(event) { return listeners.get(event)?.({ preventDefault() {}, target: this }); },
        };
        nodes.set("#" + match[3], node);
      }
    },
    querySelector(selector) { return nodes.get(selector) || null; },
  };
  const window = {
    location: { origin: "https://example.test" }, isSecureContext: options.secure !== false,
    navigator: { language: options.browserLanguage || "zh-CN", credentials: options.noPasskey ? {} : {
      async get(value) {
        credentials.push({ value, htmlAtPrompt: html });
        return options.getCredential ? options.getCredential(value) : passkeyCredential();
      },
    } },
    ...(options.preferenceLanguage ? { ExtorePreferences: { resolved: { language: options.preferenceLanguage } } } : {}),
  };
  class FixtureDate extends Date {
    static now() { return clock; }
  }
  const api = async (url, body, method, requestOptions) => {
    calls.push({ url, body, method, requestOptions });
    if (options.api) return options.api(url, body, method, requestOptions);
    if (url.endsWith("/options")) return response();
    if (url.endsWith("/verify")) return { ok: true, status: "approved" };
    throw new Error("Unexpected API request");
  };
  vm.runInNewContext(source, { window, URL, Uint8Array, atob, btoa, AbortController, Date: FixtureDate });
  const mount = (extra = {}) => window.ExtoreOwnerCli.mount({ root, requestId, api, language: options.language, ...extra });
  const controller = mount(options.mount);
  return {
    root, calls, credentials, renderings, window, controller, mount,
    node: (selector) => root.querySelector(selector),
    async review(code = "ABCD-EFGH-JKLM") {
      root.querySelector("#owner-cli-code").value = code;
      await root.querySelector("#owner-cli-code-form").emit("submit");
    },
    approve: () => root.querySelector("#owner-cli-approve").emit("click"),
    advance(seconds) { clock += seconds * 1000; },
  };
}

test("mounting a public request makes no API call or automatic Passkey prompt", () => {
  const p = fixture();
  assert.equal(p.controller.state, "code");
  assert.deepEqual(p.calls, []);
  assert.deepEqual(p.credentials, []);
  assert.match(p.root.innerHTML, /核对设备与权限/);
  assert.match(p.root.innerHTML, /核对设备不会授权/);
  assert.equal(p.node("#owner-cli-approve"), null);
});

test("review then explicit approve shows binding metadata before requesting UV Passkey", async () => {
  const p = fixture();
  await p.review(" abcd-efgh-jklm ");
  assert.equal(p.controller.state, "details");
  assert.equal(p.calls.length, 1);
  assert.deepEqual(plain(p.calls[0].body), { request_id: requestId, device_code: "ABCD-EFGH-JKLM" });
  assert.equal(p.calls[0].method, "POST");
  assert.ok(p.calls[0].requestOptions.signal instanceof AbortSignal);
  assert.deepEqual(p.credentials, []);
  assert.match(p.root.innerHTML, /AI workstation/);
  assert.match(p.root.innerHTML, new RegExp("a".repeat(64)));
  assert.match(p.root.innerHTML, /完整商家管理权限/);
  assert.match(p.root.innerHTML, /设备授权有效期（30 天）/);
  await p.approve();
  assert.equal(p.credentials.length, 1);
  assert.match(p.credentials[0].htmlAtPrompt, /完整商家管理权限/);
  assert.match(p.credentials[0].htmlAtPrompt, /ABCD-EFGH-JKLM/);
  assert.equal(p.credentials[0].value.publicKey.userVerification, "required");
  assert.deepEqual(Array.from(p.credentials[0].value.publicKey.challenge), [1, 2, 3]);
  assert.deepEqual(Array.from(p.credentials[0].value.publicKey.allowCredentials[0].id), [4, 5, 6]);
  assert.deepEqual(plain(p.calls[1].body), {
    request_id: requestId,
    credential: { id: "BAUG", rawId: "BAUG", type: "public-key",
      response: { clientDataJSON: "BwgJ", authenticatorData: "CgsM", signature: "DQ4P", userHandle: "EBES" },
      clientExtensionResults: { appid: false } },
  });
  assert.deepEqual(p.calls.map((call) => call.url), ["/auth/cli-owner/options", "/auth/cli-owner/verify"]);
  assert.equal(p.controller.state, "approved");
  assert.match(p.root.innerHTML, /不会更改当前浏览器会话/);
  for (const html of p.renderings) assert.doesNotMatch(html, /DQ4P|BwgJ|Bearer|access_token/);
});

test("browser language follows the preference module and an explicit integration language", () => {
  assert.match(fixture({ browserLanguage: "en-GB" }).root.innerHTML, /Authorize merchant CLI device/);
  assert.match(fixture({ browserLanguage: "zh-CN", preferenceLanguage: "en" }).root.innerHTML, /Authorize merchant CLI device/);
  assert.match(fixture({ preferenceLanguage: "en", language: () => "zh-CN" }).root.innerHTML, /授权商家 CLI 设备/);
});

test("invalid public request IDs never send requests", () => {
  for (const id of ["", "not-a-uuid", requestId + "?token=secret", `<img src=x onerror=alert(1)>`]) {
    const p = fixture({ mount: { requestId: id } });
    assert.equal(p.controller.state, "invalid");
    assert.equal(p.node("#owner-cli-code-form"), null);
    assert.deepEqual(p.calls, []);
    assert.doesNotMatch(p.root.innerHTML, /token=secret|onerror/);
  }
});

test("invalid or oversized device codes do not leave the browser", async () => {
  for (const code of ["", "ABCD-EFGH", "ABCD-EFGH-JKL0", "A".repeat(65), "<script>evil()</script>"]) {
    const p = fixture();
    await p.review(code);
    assert.deepEqual(p.calls, []);
    assert.deepEqual(p.credentials, []);
    assert.match(p.node("#owner-cli-error").textContent, /12 位设备码/);
    assert.equal(p.node("#owner-cli-code").focused, true);
  }
});

test("reviewed names are escaped and never interpreted as HTML", async () => {
  const p = fixture({ api: async () => response({ client_name: '<img src=x onerror="alert(1)">&device' }) });
  await p.review();
  assert.equal(p.controller.state, "details");
  assert.match(p.root.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;&amp;device/);
  assert.doesNotMatch(p.root.innerHTML, /<img|<script/);
  assert.deepEqual(p.credentials, []);
});

test("metadata must bind the exact request, device code, owner scope and bounded grant", async (t) => {
  const mutations = [
    { request_id: "f49c15c9-a1e9-49ec-8577-fe9f58b6c08a" }, { device_code: "ABCD-EFGH-JKLN" },
    { role: "staff" }, { scope: "product.owner" }, { fingerprint: "a".repeat(63) },
    { fingerprint: "<img src=x>" }, { client_name: "A".repeat(201) }, { expires: now - 1 },
    { grant_expires: now - 1 }, { grant_expires: now + 31 * 86400 },
    { expires: now + 631 }, { expires: Infinity }, { expires: NaN }, { expires: String(now + 300) },
    { grant_expires: Infinity }, { grant_expires: NaN }, { grant_expires: String(now + 30 * 86400) },
    { options: { ...response().options, rpId: "other.test" } },
    { options: { ...response().options, userVerification: "preferred" } },
    { options: { ...response().options, challenge: "<script>" } },
    { options: { ...response().options, allowCredentials: null } },
    { options: { ...response().options, allowCredentials: "BAUG" } },
    { options: { ...response().options, allowCredentials: Array.from({ length: 129 }, () => ({ type: "public-key", id: "BAUG" })) } },
    { options: { ...response().options, allowCredentials: [{ type: "password", id: "BAUG" }] } },
    { options: { ...response().options, allowCredentials: [{ type: "public-key", id: "bad=id" }] } },
  ];
  for (const [index, mutation] of mutations.entries()) await t.test(String(index), async () => {
    const p = fixture({ api: async () => response(mutation) });
    await p.review();
    assert.ok(["error", "expired"].includes(p.controller.state));
    assert.equal(p.node("#owner-cli-approve"), null);
    assert.deepEqual(p.credentials, []);
    assert.equal(p.calls.length, 1);
  });
});

test("the backend's fractional timestamps and discoverable Passkey options can be approved", async () => {
  // owner_cli_auth uses time.time() and generate_authentication_options without allow_credentials.
  // options_to_json therefore emits a valid empty allowCredentials list for resident Passkeys.
  const backendShape = response({
    expires: now + 599.876543, grant_expires: now + 30 * 86400 - 0.123457,
    options: { challenge: "hwdBQvqRNHEkvB8nzMP4AvTuxsbexdaFDCkbTP89WQs", timeout: 60000,
      rpId: "example.test", allowCredentials: [], userVerification: "required" },
  });
  const p = fixture({ api: async (url) => url.endsWith("/options") ? backendShape : { ok: true, status: "approved" } });
  await p.review();
  assert.equal(p.controller.state, "details");
  assert.equal(p.credentials.length, 0);
  await p.approve();
  assert.equal(p.controller.state, "approved");
  assert.equal(p.credentials[0].value.publicKey.userVerification, "required");
  assert.equal(p.credentials[0].value.publicKey.rpId, "example.test");
  assert.deepEqual(plain(p.credentials[0].value.publicKey.allowCredentials), []);
  assert.equal(p.calls.length, 2);
});

test("omitted allowCredentials remains omitted for discoverable Passkey authentication", async () => {
  const options = { ...response().options };
  delete options.allowCredentials;
  const p = fixture({ api: async (url) => url.endsWith("/options") ? response({ options }) : { ok: true, status: "approved" } });
  await p.review();
  assert.equal(p.controller.state, "details");
  await p.approve();
  assert.equal(p.controller.state, "approved");
  assert.equal(Object.hasOwn(p.credentials[0].value.publicKey, "allowCredentials"), false);
  assert.equal(p.credentials[0].value.publicKey.userVerification, "required");
});

test("cancel clears the reviewed request without verifying or granting", async () => {
  const p = fixture();
  await p.review();
  p.node("#owner-cli-cancel").emit("click");
  assert.equal(p.controller.state, "cancelled");
  assert.match(p.root.innerHTML, /设备尚未获得授权/);
  assert.deepEqual(p.credentials, []);
  assert.equal(p.calls.length, 1);
  p.node("#owner-cli-again").emit("click");
  assert.equal(p.controller.state, "code");
  assert.equal(p.node("#owner-cli-code").value, "");
  assert.equal(p.calls.length, 1);
});

test("an expired reviewed request cannot prompt a Passkey or verify", async () => {
  const p = fixture();
  await p.review();
  p.advance(301);
  await p.approve();
  assert.equal(p.controller.state, "expired");
  assert.match(p.root.innerHTML, /请在 CLI 中重新发起/);
  assert.deepEqual(p.credentials, []);
  assert.equal(p.calls.length, 1);
});

test("unsupported or insecure browsers remain unapproved", async () => {
  for (const option of [{ noPasskey: true }, { secure: false }]) {
    const p = fixture(option);
    await p.review();
    await p.approve();
    assert.equal(p.controller.state, "details");
    assert.match(p.root.innerHTML, /当前浏览器无法使用 Passkey/);
    assert.deepEqual(p.credentials, []);
    assert.equal(p.calls.length, 1);
  }
});

test("Passkey cancellation requires a fresh explicit review, with no verification request", async () => {
  const p = fixture({ getCredential() { throw Object.assign(new Error("cancelled"), { name: "NotAllowedError" }); } });
  await p.review();
  await p.approve();
  assert.equal(p.controller.state, "cancelled");
  assert.match(p.root.innerHTML, /Passkey 验证已取消或超时/);
  assert.equal(p.node("#owner-cli-approve"), null);
  assert.equal(p.calls.length, 1);
  await p.review();
  assert.equal(p.controller.state, "details");
  assert.equal(p.calls.length, 2);
  assert.equal(p.credentials.length, 1);
});

test("verification rejection or expiry never reports successful approval", async () => {
  for (const message of ["授权请求已被拒绝", "请求已过期", "审批校验失败"]) {
    const p = fixture({ api: async (url) => {
      if (url.endsWith("/options")) return response();
      throw new Error(message);
    } });
    await p.review();
    await p.approve();
    assert.equal(p.controller.state, message.includes("拒绝") ? "denied" : message.includes("过期") ? "expired" : "error");
    assert.match(p.root.innerHTML, new RegExp(message));
    assert.doesNotMatch(p.root.innerHTML, /<h2>设备已授权/);
    assert.equal(p.node("#owner-cli-approve"), null);
  }
});

test("an unconfirmed verification response cannot display success", async () => {
  const p = fixture({ api: async (url) => url.endsWith("/options") ? response() : { ok: true, status: "pending" } });
  await p.review();
  await p.approve();
  assert.equal(p.controller.state, "error");
  assert.match(p.root.innerHTML, /服务器未确认授权/);
});

test("repeated review submissions and approval clicks do not duplicate requests", async () => {
  const options = deferred(), credential = deferred();
  const p = fixture({ api: async (url) => url.endsWith("/options") ? options.promise : { ok: true, status: "approved" }, getCredential: () => credential.promise });
  const first = p.review();
  await p.review();
  assert.equal(p.calls.length, 1);
  assert.equal(p.node("#owner-cli-review").disabled, true);
  options.resolve(response());
  await first;
  const approving = p.approve();
  await p.approve();
  assert.equal(p.credentials.length, 1);
  assert.equal(p.node("#owner-cli-approve").disabled, true);
  assert.equal(p.node("#owner-cli-cancel").disabled, true);
  credential.resolve(passkeyCredential());
  await approving;
  assert.equal(p.calls.length, 2);
  assert.equal(p.controller.state, "approved");
});

test("disposing an in-flight review aborts it and cannot replace a newer page", async () => {
  const options = deferred();
  const p = fixture({ api: async () => options.promise });
  const reviewing = p.review();
  p.controller.dispose();
  assert.equal(p.calls[0].requestOptions.signal.aborted, true);
  p.root.innerHTML = "New route";
  options.resolve(response());
  await reviewing;
  assert.equal(p.root.innerHTML, "New route");
  assert.deepEqual(p.credentials, []);
});

test("disposing during Passkey verification cannot authorize or alter another route", async () => {
  const credential = deferred();
  const p = fixture({ getCredential: () => credential.promise });
  await p.review();
  const approving = p.approve();
  p.controller.dispose();
  assert.equal(p.credentials[0].value.signal.aborted, true);
  p.root.innerHTML = "New route";
  credential.resolve(passkeyCredential());
  await approving;
  assert.equal(p.calls.length, 1);
  assert.equal(p.root.innerHTML, "New route");
});

test("remounting the same element disposes the prior approval controller", async () => {
  const options = deferred();
  const p = fixture({ api: async () => options.promise });
  const reviewing = p.review();
  const current = p.mount({ requestId: "f49c15c9-a1e9-49ec-8577-fe9f58b6c08a" });
  assert.equal(p.calls[0].requestOptions.signal.aborted, true);
  const html = p.root.innerHTML;
  options.resolve(response());
  await reviewing;
  assert.equal(p.root.innerHTML, html);
  assert.equal(current.state, "code");
});
