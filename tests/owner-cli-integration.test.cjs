const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { appFixture, flush, product } = require("./app-fixture.cjs");

const requestId = "36fc7bd8-a3e2-4f33-854b-7fe8c35d970c";
const secondRequestId = "f49c15c9-a1e9-49ec-8577-fe9f58b6c08a";

function ownerPage() {
  const page = appFixture();
  const mounts = [];
  const controllers = [];
  const events = [];
  page.context.window.ExtoreOwnerCli = {
    mount(options) {
      mounts.push(options);
      events.push("mount:" + options.requestId);
      options.root.innerHTML = "Owner approval for " + options.requestId;
      const controller = {
        disposed: 0,
        dispose() {
          this.disposed++;
          events.push("dispose:" + options.requestId);
        },
      };
      controllers.push(controller);
      return controller;
    },
  };
  page.navigate("/cli/owner#" + requestId);
  return { ...page, mounts, controllers, events };
}

test("the public owner approval route mounts before any session request", async () => {
  const page = ownerPage();
  await page.context.start();
  assert.equal(page.requests.length, 0);
  assert.equal(page.mounts.length, 1);
  const mount = page.mounts[0];
  assert.equal(mount.root, page.node("#app"));
  assert.equal(mount.requestId, requestId);
  assert.equal(typeof mount.api, "function");
  assert.equal(typeof mount.language, "function");
  assert.equal(mount.language(), "zh-CN");
  page.set({ lang: "en" });
  assert.equal(mount.language(), "en");
  assert.equal(page.getContext().page, "owner_cli");
  assert.match(page.node("#header-context").textContent, /商家 CLI/);
});

test("the mounted approval component receives the normal API adapter with cancellation", async () => {
  const page = ownerPage();
  await page.context.start();
  const controller = new AbortController();
  const body = { request_id: requestId, device_code: "ABCD-EFGH-JKLM" };
  const pending = page.mounts[0].api("/auth/cli-owner/options", body, "POST", { signal: controller.signal });
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/auth/cli-owner/options");
  assert.equal(page.requests[0].options.method, "POST");
  assert.equal(page.requests[0].options.signal, controller.signal);
  assert.deepEqual(page.requests[0].body, body);
  const response = { request_id: requestId, device_code: body.device_code };
  page.requests[0].respond(response);
  assert.equal(await pending, response);
});

test("restarting an approval disposes the old controller before mounting another", async () => {
  const page = ownerPage();
  await page.context.start();
  await page.context.start();
  page.navigate("/cli/owner#" + secondRequestId);
  await page.context.start();
  assert.deepEqual(page.events, [
    "mount:" + requestId,
    "dispose:" + requestId,
    "mount:" + requestId,
    "dispose:" + requestId,
    "mount:" + secondRequestId,
  ]);
  assert.deepEqual(page.controllers.map((controller) => controller.disposed), [1, 1, 0]);
  assert.equal(page.requests.length, 0);
  assert.equal(page.mounts[2].requestId, secondRequestId);
});

test("leaving the approval route disposes it before loading the destination", async () => {
  const page = ownerPage();
  await page.context.start();
  page.navigate("/");
  const leaving = page.context.start();
  assert.equal(page.controllers[0].disposed, 1);
  assert.deepEqual(page.events, ["mount:" + requestId, "dispose:" + requestId]);
  assert.equal(page.requests[0].url, "/api/auth/status");
  page.requests[0].respond({ role: null });
  await flush();
  assert.equal(page.requests[1].url, "/api/products");
  page.requests[1].respond([]);
  await leaving;
  assert.equal(page.mounts.length, 1);
  assert.equal(page.getContext().page, "home");
  assert.equal(page.node("#header-context").textContent, "兑换与领取");
  assert.equal(page.requests.some((request) => request.url === "/api/staff/login"), false);
});

test("owner approval clears a previous staff and receipt context without upgrading its session", async () => {
  const page = ownerPage();
  const privateProduct = product("PRIVATE_CUSTOMER_PRODUCT");
  page.set({
    role: "staff",
    permissions: ["queue.view", "queue.process", "cards.manage"],
    managedProductId: "old-managed-product",
    currentToken: "PRIVATE_CUSTOMER_TOKEN",
    currentProduct: privateProduct,
    currentBatch: { items: [{ card_id: "PRIVATE_CARD_ID", product: privateProduct }] },
    currentJob: { content: "PRIVATE_DELIVERY_CONTENT" },
    currentVariant: { id: "PRIVATE_VARIANT" },
    batchSelection: "PRIVATE_CARD_ID",
    queueProduct: privateProduct,
  });
  await page.context.start();
  const context = page.getContext();
  assert.equal(context.page, "owner_cli");
  assert.equal(context.role, null);
  assert.equal(context.currentToken, "");
  assert.equal(context.product, null);
  assert.equal(context.queueProduct, null);
  assert.equal(context.batch, false);
  assert.equal(context.cardId, "");
  assert.equal(context.productId, null);
  assert.equal(context.permissions.length, 0);
  assert.doesNotMatch(JSON.stringify(context), /PRIVATE_/);
  assert.equal(page.requests.length, 0);
  assert.equal(page.requests.some((request) => request.url === "/api/staff/login"), false);
});

test("an earlier session response cannot replace a newly mounted owner approval", async () => {
  const page = ownerPage();
  page.navigate("/admin");
  const oldStart = page.context.start();
  assert.equal(page.requests[0].url, "/api/auth/status");
  page.navigate("/cli/owner#" + requestId);
  await page.context.start();
  const approval = page.node("#app").innerHTML;
  page.requests[0].respond({ role: "admin", configured: true, password_enabled: false });
  await oldStart;
  assert.equal(page.requests.length, 1);
  assert.equal(page.node("#app").innerHTML, approval);
  assert.equal(page.controllers[0].disposed, 0);
  assert.equal(page.getContext().page, "owner_cli");
  assert.equal(page.getContext().role, null);
});

test("an earlier public product response cannot replace a newly mounted owner approval", async () => {
  const page = ownerPage();
  page.navigate("/");
  const oldStart = page.context.start();
  page.requests[0].respond({ role: null });
  await flush();
  assert.equal(page.requests[1].url, "/api/products");
  page.navigate("/cli/owner#" + requestId);
  await page.context.start();
  const approval = page.node("#app").innerHTML;
  page.requests[1].respond([]);
  await oldStart;
  assert.equal(page.node("#app").innerHTML, approval);
  assert.equal(page.controllers[0].disposed, 0);
  assert.equal(page.getContext().page, "owner_cli");
});

test("late admin and staff requests cannot restore their context over owner approval", async (t) => {
  const ownerAuth = { role: "admin", configured: true, password_enabled: false };
  const staffAuth = { role: "staff", permissions: ["product.edit"], product_id: "managed-product" };
  const cases = [
    { name: "admin authentication", route: "/admin", auth: ownerAuth, expected: "/api/auth/status", response: ownerAuth },
    { name: "admin products", route: "/admin", auth: ownerAuth, expected: "/api/admin/products", response: [product("PRIVATE_ADMIN_PRODUCT")], products: true },
    { name: "staff authentication", route: "/staff", auth: staffAuth, expected: "/api/auth/status", response: staffAuth },
    { name: "staff link login", route: "/staff#old-management-grant", auth: staffAuth, expected: "/api/staff/login", response: { ok: true } },
  ];
  for (const scenario of cases) await t.test(scenario.name, async () => {
    const page = ownerPage();
    page.navigate(scenario.route);
    const oldStart = page.context.start();
    page.requests[0].respond(scenario.auth);
    await flush();
    if (scenario.products) {
      page.requests[1].respond(ownerAuth);
      await flush();
    }
    const pendingIndex = page.requests.length - 1;
    assert.equal(page.requests[pendingIndex].url, scenario.expected);
    page.navigate("/cli/owner#" + requestId);
    await page.context.start();
    const approval = page.node("#app").innerHTML;
    page.requests[pendingIndex].respond(scenario.response);
    await oldStart;
    assert.equal(page.requests.length, pendingIndex + 1);
    assert.equal(page.node("#app").innerHTML, approval);
    assert.equal(page.controllers[0].disposed, 0);
    assert.equal(page.getContext().page, "owner_cli");
    assert.equal(page.getContext().role, null);
    assert.equal(page.getContext().productId, null);
    assert.doesNotMatch(JSON.stringify(page.getContext()), /PRIVATE_/);
  });
});

test("approval and QR modules load before app startup with revised assets", () => {
  const html = fs.readFileSync(path.join(__dirname, "../extore/static/index.html"), "utf8");
  const scripts = [...html.matchAll(/<script\b([^>]*\bsrc="([^"]+)"[^>]*)>/g)];
  const owner = scripts.findIndex((script) => script[2].startsWith("/static/owner-cli.js"));
  const app = scripts.findIndex((script) => script[2].startsWith("/static/app.js"));
  const account = scripts.findIndex((script) => script[2].startsWith("/static/account.js"));
  assert.ok(owner >= 0 && account > owner && app > account);
  assert.match(scripts[owner][1], /\bdefer\b/);
  assert.match(scripts[owner][2], /\?v=20261007-shops$/);
  assert.match(scripts[app][2], /\?v=20261008-processor-contributions$/);
  const device = scripts.findIndex((script) => script[2].startsWith("/static/device-login.js"));
  const encoder = scripts.findIndex((script) => script[2].startsWith("/static/vendor/qrcodegen.js"));
  const qr = scripts.findIndex((script) => script[2].startsWith("/static/totp-qr.js"));
  assert.ok(encoder >= 0 && qr > encoder && account > qr && device > account && app > device);
  assert.match(scripts[account][2], /\?v=20261008-processor-contributions$/);
  assert.match(scripts[device][2], /\?v=20261007-queue-design$/);
});

test("merchant dashboard displays escaped shop name and email and refreshes without replacing the form", () => {
  const page = appFixture();
  page.navigate("/admin");
  page.context.window.ExtoreAccount = { rootScope: (auth) => auth.role === "admin" && auth.superadmin === true && auth.shop_id === null };
  const auth = { role: "admin", shop_id: "shop-identity", superadmin: false, shop_name: '店铺 <script>alert(1)</script>', shop_email: 'owner+tag@example.test', session_id: "session-identity" };
  page.context.acceptAuth(auth);
  page.context.shell();
  assert.match(page.node("#app").innerHTML, /店铺 &lt;script&gt;alert\(1\)&lt;\/script&gt; · owner\+tag@example\.test/);
  assert.doesNotMatch(page.node("#app").innerHTML, /店铺 · shop-identity/);
  const shell = page.node("#app").innerHTML;
  page.node("#workspace").innerHTML = '<form id="unsaved-product">unsaved</form>';
  page.context.acceptAuth({ ...auth, shop_name: '改名后的店铺', shop_email: 'updated@example.test' });
  assert.equal(page.node("#management-identity").textContent, '改名后的店铺 · updated@example.test');
  assert.equal(page.node("#app").innerHTML, shell);
  assert.equal(page.node("#workspace").innerHTML, '<form id="unsaved-product">unsaved</form>');
  page.context.acceptAuth({ role: "admin", shop_id: null, superadmin: true, shop_name: 'stale shop', shop_email: 'stale@example.test' });
  assert.equal(page.node("#management-identity").textContent, '超级管理员 · 平台范围');
});
