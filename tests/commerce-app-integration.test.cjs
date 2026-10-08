const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture } = require("./app-fixture.cjs");

const requestId = "r".repeat(43);
const owner = { role: "admin", shop_id: "shop-one", superadmin: false, session_id: "session-one" };
function commercePage() {
  const page = appFixture(), mounts = [], controllers = [];
  page.context.window.ExtoreCommerceConnect = {
    mount(options) {
      mounts.push(options);
      const instance = { disposed: false, dispose() { this.disposed = true; } };
      controllers.push(instance);
      return instance;
    },
  };
  return { ...page, mounts, controllers };
}

test("merchant navigation offers storefront connections but a product manager never receives it", () => {
  const page = commercePage();
  page.context.acceptAuth(owner);
  page.context.shell();
  assert.match(page.node("#app").innerHTML, /data-tab="commerce"/);
  assert.match(page.node("#app").innerHTML, /id="management-section"/);
  assert.match(page.node("#app").innerHTML, /optgroup label="日常经营"/);
  page.context.acceptAuth({ role: "staff", permissions: ["queue.view"], product_id: "one" });
  page.context.shell();
  assert.doesNotMatch(page.node("#app").innerHTML, /data-tab="commerce"|value="commerce"/);
  assert.doesNotMatch(page.node("#app").innerHTML, /data-tab="profiles"|data-tab="cards"/);
});

test("the mobile selector mounts the same scoped management view and retains focus", async () => {
  const page = commercePage();
  page.navigate("/admin");
  page.context.acceptAuth(owner);
  page.context.shell();
  page.node("#management-section").value = "commerce";
  await page.node("#management-section").emit("change");
  assert.equal(page.mounts.length, 1);
  assert.equal(page.mounts[0].mode, "management");
  assert.equal(page.mounts[0].auth(), owner);
  assert.equal(page.mounts[0].isCurrent(), true);
  assert.equal(page.node("#management-section").focused, true);
  page.set({ tab: "cards" });
  assert.equal(page.mounts[0].isCurrent(), false);
});

test("the approval route clears redemption context and does not approve on entry", async () => {
  const page = commercePage();
  page.navigate("/connect/authorize#" + requestId);
  page.set({ currentToken: "CUSTOMER_SECRET", currentProduct: { id: "private" }, currentJob: { content: "PRIVATE_OUTPUT" }, currentBatch: { items: [] } });
  const pending = page.context.start();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/auth/status");
  page.requests[0].respond(owner);
  await pending;
  assert.equal(page.mounts.length, 1);
  assert.equal(page.mounts[0].mode, "authorize");
  assert.equal(page.mounts[0].requestId, requestId);
  assert.equal(page.getContext().page, "commerce_authorize");
  assert.equal(page.getContext().currentToken, "");
  assert.equal(page.getContext().product, null);
  assert.equal(page.requests.length, 1);
});

test("navigation disposes an approval and invalidates its API continuation", async () => {
  const page = commercePage();
  page.navigate("/connect/authorize#" + requestId);
  const first = page.context.start();
  page.requests[0].respond(owner);
  await first;
  page.navigate("/connect/authorize#" + "s".repeat(43));
  const second = page.context.start();
  assert.equal(page.controllers[0].disposed, true);
  assert.equal(page.mounts[0].isCurrent(), false);
  page.requests[1].respond(owner);
  await second;
  assert.equal(page.mounts[1].requestId, "s".repeat(43));
});

test("login returns only to a complete local authorization request", async () => {
  for (const destination of ["/connect/authorize#" + requestId, "https://evil.invalid/connect/authorize#" + requestId, "/connect/authorize#short", "//evil.invalid/", "/admin"]) {
    const page = commercePage(), mounts = [], destinations = [];
    page.context.window.ExtoreAccount = { route: () => ({ mode: "login" }), mount(options) { mounts.push(options); return { dispose() {} }; } };
    page.context.navigate = (url) => { destinations.push(url); };
    page.navigate("/account/login");
    page.context.location.search = "?return_to=" + encodeURIComponent(destination);
    const pending = page.context.start();
    page.requests[0].respond(owner);
    await pending;
    mounts[0].navigate("/admin");
    assert.equal(destinations[0], destination === "/connect/authorize#" + requestId ? destination : "/admin");
  }
});

test("fresh verification resolves only after completion and cancellation disposes the credentials form", async () => {
  for (const complete of [true, false]) {
    const page = commercePage(), root = page.node("#fresh"), mounts = [];
    root.removeEventListener = (event) => root.listeners.delete(event);
    page.context.acceptAuth(owner);
    let handler, disposed = false;
    page.context.window.ExtoreAccount = { mount(options) { mounts.push(options); return {
      confirmFresh(callback) { handler = callback; root.innerHTML = "credential form"; },
      dispose() { disposed = true; },
    }; } };
    const pending = page.context.reauthenticateCommerce({ root, title: "Approve storefront", isCurrent: () => true });
    assert.equal(root.innerHTML, "credential form");
    assert.equal(mounts[0].mode, "confirm");
    if (complete) handler();
    else await root.emit("click", { target: { closest: () => ({}) } });
    assert.equal(await pending, complete ? owner : false);
    assert.equal(disposed, true);
    assert.equal(root.innerHTML, "");
  }
});
