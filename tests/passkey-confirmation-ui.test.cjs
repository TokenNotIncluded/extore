const test = require("node:test");
const assert = require("node:assert/strict");
const { appFixture, flush } = require("./app-fixture.cjs");

function browser() {
  const page = appFixture();
  page.navigate("/admin");
  let requested;
  page.context.navigator.credentials = { async get(options) {
    requested = options;
    return { getClientExtensionResults: () => ({}), id: "a2V5", rawId: new Uint8Array([107, 101, 121]).buffer, type: "public-key",
      response: { clientDataJSON: new Uint8Array([1]).buffer, authenticatorData: new Uint8Array([2]).buffer,
        signature: new Uint8Array([3]).buffer, userHandle: null } };
  } };
  return { ...page, requested: () => requested };
}

test("confirmation sends only session-bound reauthentication endpoints and retains allowCredentials", async () => {
  const page = browser();
  const pending = page.context.passkey(false, null, { reauthenticate: true, expectedScope: "platform", expectedSessionId: "captured-session" });
  assert.equal(page.requests[0].url, "/api/auth/reauth/passkey/options");
  page.requests[0].respond({ challenge: "Y2hhbGxlbmdl", allowCredentials: [{ type: "public-key", id: "a2V5" }], userVerification: "required" });
  await flush();
  assert.equal(page.requested().publicKey.userVerification, "required");
  assert.equal(page.requested().publicKey.allowCredentials[0].id.byteLength, 3);
  assert.equal(page.requests[1].url, "/api/auth/reauth/passkey/verify");
  for (const request of page.requests.slice(0, 2)) {
    assert.equal(request.options.headers["X-Extore-Shop-Scope"], "platform");
    assert.equal(request.options.headers["X-Extore-Session-ID"], "captured-session");
  }
  page.requests[1].respond({ ok: true }); await flush();
  assert.equal(page.requests[2].url, "/api/auth/status");
  page.requests[2].respond({ role: "admin", session_id: "captured-session" });
  assert.equal((await pending).session_id, "captured-session");
  assert.equal(page.requests.some((request) => request.url.includes("/login/")), false);
});

test("a failed confirmation stops before status or a business write and never retries as login", async () => {
  const page = browser();
  const pending = page.context.passkey(false, null, { reauthenticate: true, expectedScope: "shop-one", expectedSessionId: "current" });
  const rejected = assert.rejects(pending, /wrong account/);
  page.requests[0].respond({ challenge: "Y2hhbGxlbmdl", allowCredentials: [{ type: "public-key", id: "a2V5" }] }); await flush();
  page.requests[1].reject(new Error("wrong account"));
  await rejected;
  assert.equal(page.requests.length, 2);
  assert.ok(page.requests.every((request) => request.url.startsWith("/api/auth/reauth/passkey/")));
});

test("explicit sign-in still uses the discoverable login ceremony", async () => {
  const page = browser();
  const pending = page.context.passkey();
  assert.equal(page.requests[0].url, "/api/auth/login/options");
  page.requests[0].respond({ challenge: "Y2hhbGxlbmdl" }); await flush();
  assert.equal(page.requests[1].url, "/api/auth/login/verify");
  page.requests[1].respond({ role: "admin" }); await flush();
  page.requests[2].respond({ role: "admin", shop_id: "chosen-account" });
  assert.equal((await pending).shop_id, "chosen-account");
});
