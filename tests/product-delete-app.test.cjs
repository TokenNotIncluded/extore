const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush, product, job } = require("./app-fixture.cjs");

test("a delete-only product manager gets a safe list without editing or fulfillment authority", async () => {
  const page = appFixture();
  page.navigate("/staff");
  page.set({ role: "staff", permissions: ["product.delete"], managedProductId: "product", managementLinkId: "link", tab: "products" });
  let received;
  page.context.window.ExtoreProducts = { render(ctx) { received = ctx; } };
  assert.ok(page.context.managementTabs().some(([key]) => key === "products"));
  const rendering = page.context.renderTab();
  assert.equal(page.requests[0].url, "/api/manage/products?view=active");
  page.requests[0].respond([product()]);
  await rendering;
  assert.equal(received.canDelete, true);
  assert.equal(received.canEdit, false);
  assert.equal(received.canConfigure, false);
  assert.equal(page.requests.some((request) => request.url === "/api/manage/product"), false);
});

test("a late recycle-bin list cannot update the product view after leaving its shop", async () => {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ role: "admin", tab: "products", authStatus: { session_id: "session-one", shop_id: "shop-one" } });
  let renders = 0;
  page.context.window.ExtoreProducts = { render() { renders++; } };
  const rendering = page.context.renderProducts("deleted", true);
  assert.equal(page.requests[0].url, "/api/admin/products?view=deleted");
  page.set({ tab: "jobs", authStatus: { session_id: "session-two", shop_id: "shop-two" } });
  page.requests[0].respond([product({ deleted: true })]);
  await rendering;
  assert.equal(renders, 0);
});

test("a failed product-list refresh restores a usable current view and reports the failure", async () => {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ role: "admin", tab: "products", products: [product()] });
  let received;
  page.context.window.ExtoreProducts = { render(ctx) { received = ctx; } };
  const rendering = page.context.renderProducts("deleted", true);
  page.requests[0].reject(new Error("Network unavailable"));
  await rendering;
  assert.equal(page.getContext().productsView, "active");
  assert.equal(received.isCurrent(), true);
  assert.equal(received.products.length, 1);
  assert.match(page.node("#error").textContent, /加载失败.*Network unavailable/);
});

test("deleted products remain selectable for their unfinished task queues", async () => {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ role: "admin", tab: "jobs" });
  const rendering = page.context.renderJobs("", "product");
  assert.equal(page.requests[0].url, "/api/manage/products?view=all");
  page.requests[0].respond([{ ...product(), deleted: true, deleted_at: 123 }]);
  await flush();
  page.requests[1].respond([{ ...job("task", "queued"), product_id: "product", params: {}, files: [] }]);
  await rendering;
  assert.equal(page.getContext().queueProductId, "product");
  assert.match(page.node("#workspace").innerHTML, /已删除/);
  assert.match(page.node("#workspace").innerHTML, /task/);
});

test("card history receives deleted products without replacing the active product cache", async () => {
  const page = appFixture();
  page.navigate("/admin");
  const active = product();
  const deleted = { ...product(), id: "deleted-product", deleted: true, deleted_at: 123 };
  page.set({ role: "admin", tab: "cards", products: [active], cardProductId: deleted.id });
  let received;
  page.context.window.ExtoreCards = { render(ctx) { received = ctx; } };
  const rendering = page.context.renderCards();
  assert.equal(page.requests[0].url, "/api/admin/products?view=all");
  page.requests[0].respond([active, deleted]);
  await rendering;
  assert.equal(received.productId, deleted.id);
  assert.equal(received.products.length, 2);
  assert.equal(received.products[1].deleted, true);
  assert.equal(page.context.productUIContext().products.length, 1);
});

test("restoring from the recycle bin merges active products without discarding other products", () => {
  const page = appFixture();
  page.navigate("/admin");
  const active = product();
  const deleted = { ...product(), id: "deleted-product", deleted: true, deleted_at: 123 };
  page.set({ role: "admin", tab: "products", productsView: "deleted", products: [active] });
  const ctx = page.context.productUIContext([deleted]);
  ctx.onSaved([{ ...deleted, deleted: false, deleted_at: null }]);
  assert.equal(page.context.productUIContext().products.length, 2);
  ctx.onSaved([deleted]);
  assert.equal(page.context.productUIContext().products.length, 1);
  assert.equal(page.context.productUIContext().products[0].id, active.id);
});
