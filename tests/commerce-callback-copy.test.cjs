"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { setImmediate } = require("node:timers/promises");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/commerce-connect.js"), "utf8");
const shopId = "11111111-1111-4111-8111-111111111111";

async function managementPage(t, language, clients = []) {
  const root = {
    innerHTML: "",
    isConnected: true,
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const window = {};
  const calls = [];
  const context = vm.createContext({ window, URL, URLSearchParams, AbortController, structuredClone });
  vm.runInContext(source, context);
  const instance = window.ExtoreCommerceConnect.mount({
    root,
    language,
    auth: { role: "admin", session_id: "test-session", shop_id: shopId, superadmin: false },
    async api(url, body, method) {
      calls.push({ url, body, method });
      const request = new URL(url, "https://extore.example");
      assert.equal(method, "GET");
      assert.equal(body, undefined);
      assert.equal(request.searchParams.get("shop_id"), shopId);
      assert.equal(request.searchParams.get("view"), "active");
      if (request.pathname === "/admin/commerce/clients") return { clients };
      assert.equal(request.pathname, "/admin/commerce/grants");
      return { grants: [] };
    },
  });
  t.after(() => instance.dispose());
  await setImmediate();
  assert.equal(calls.length, 2);
  return root.innerHTML;
}

for (const [language, instruction] of [
  ["zh-CN", "请从目标商城复制完整回调地址，示例不是固定地址。"],
  ["en-GB", "Copy the full callback URL from your marketplace; the example is not a fixed address."],
]) {
  test(`callback example uses /store/manage without filling the form (${language})`, async (t) => {
    const html = await managementPage(t, language);
    assert.match(html, /<textarea\b[^>]*placeholder="https:\/\/store\.example\/store\/manage"[^>]*><\/textarea>/);
    assert.ok(html.includes(instruction));
    assert.ok(!html.includes("/connect/callback"));
  });
}

test("registered callback URLs remain unchanged and are not replaced by the example", async (t) => {
  const callback = "https://other.example/custom/return";
  const clients = [{ client_id: "existing-client", client_name: "Existing store", shop_id: shopId,
    redirect_uris: [callback], enabled: true }];
  const original = structuredClone(clients);
  const html = await managementPage(t, "en-GB", clients);
  assert.ok(html.includes(`<dd>${callback}</dd>`));
  assert.deepEqual(clients, original);
});
