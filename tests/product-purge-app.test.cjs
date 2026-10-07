const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, product } = require("./app-fixture.cjs");

test("purge-only navigation obtains safe product summaries without editing permission", async () => {
  const page = appFixture();
  page.navigate("/staff");
  page.set({ role: "staff", permissions: ["product.purge"], managedProductId: "product", managementLinkId: "link", tab: "products" });
  let received;
  page.context.window.ExtoreProducts = { render(ctx) { received = ctx; } };
  assert.ok(page.context.managementTabs().some(([key]) => key === "products"));
  const rendering = page.context.renderTab();
  assert.equal(page.requests[0].url, "/api/manage/products?view=active");
  page.requests[0].respond([product()]);
  await rendering;
  assert.equal(received.canPurge, true);
  assert.equal(received.canDelete, false);
  assert.equal(received.canEdit, false);
});

test("purge cache invalidation preserves old queues while blocking stale product capabilities", async () => {
  const page = appFixture();
  page.navigate("/staff");
  page.set({ role: "staff", permissions: ["product.purge", "queue.view"], managedProductId: "product", managementLinkId: "link", tab: "products", products: [product()], queueProduct: product(), authStatus: { session_id: "session-one", role: "staff" } });
  await page.getActions().productsPurged(["product"]);
  assert.equal(page.getContext().productPurged, true);
  assert.equal(page.getContext().productDeleted, true);
  assert.equal(page.getContext().queueProduct.id, "product");
  assert.equal(page.getContext().queueProduct.purged, true);
  assert.equal(page.context.productUIContext().products.length, 0);
  page.context.acceptAuth({ role: "staff", product_id: "product", link_id: "link", session_id: "session-one", permissions: ["product.purge", "queue.view"] });
  assert.equal(page.getContext().productPurged, true);
  page.context.acceptAuth({ role: "staff", product_id: "other", link_id: "other", session_id: "session-two", permissions: ["queue.view"] });
  assert.equal(page.getContext().productPurged, false);
});
