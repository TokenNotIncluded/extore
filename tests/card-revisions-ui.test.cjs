const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush, product, job } = require("./app-fixture.cjs");

const entitlement = (remaining = 1) => ({ attribute_key: "edit_quota", label: { "zh-CN": "修改机会" }, total: 1, used: 1 - remaining, remaining, can_request: remaining > 0, reason: remaining ? null : "exhausted" });
const task = (overrides = {}) => ({ ...job("revision-job"), revision: { current: 0, message: "", is_revision: false }, entitlements: entitlement(), card_attributes: { edit_quota: 1, plan: "plus" }, deliveries: [{ revision: 0, attempt: 1, created: 1, has_files: false, revealed: false }], last_delivery: { revision: 0, attempt: 1, created: 1 }, ...overrides });
function page() {
  const p = appFixture();
  p.context.crypto = require("node:crypto").webcrypto;
  p.set({ currentToken: "revision-token", currentProduct: product("Revision product"), currentBatch: null });
  p.navigate("/receipt#revision-token");
  return p;
}

test("顾客实际提交修改表单，经队列刷新后仍显示原交付入口", async () => {
  const p = page();
  p.context.renderReceipt(task());
  p.node("#revision-message").value = "补充实验的讨论段";
  const submitting = p.node("#receipt-revision-form").emit("submit");
  assert.equal(p.requests[0].url, "/api/receipt/revisions");
  assert.equal(p.requests[0].body.message, "补充实验的讨论段");
  assert.equal(p.node("#request-revision").disabled, true);
  const queued = task({ state: "queued", revision: { current: 1, message: "补充实验的讨论段", is_revision: true }, entitlements: { ...entitlement(0), reason: "in_progress" } });
  p.requests[0].respond(queued);
  await flush();
  p.requests[1].respond({ product: product("Revision product"), job: queued });
  await submitting;
  assert.equal(p.node("#error").textContent, "");
  assert.match(p.node("#app").innerHTML, /第 1 轮修改/);
  assert.match(p.node("#app").innerHTML, /之前的交付仍可查看/);
  assert.match(p.node("#app").innerHTML, /id="reveal"/);
});

test("修改成功而刷新网络失败不误报提交失败，禁止旧表单再次消费额度", async () => {
  const p = page();
  p.context.renderReceipt(task());
  p.node("#revision-message").value = "优化标题";
  const submitting = p.node("#receipt-revision-form").emit("submit");
  p.requests[0].respond(task({ state: "queued", revision: { current: 1, message: "优化标题", is_revision: true }, entitlements: entitlement(0) }));
  await flush();
  p.requests[1].reject(new Error("status refresh offline"));
  await submitting;
  assert.equal(p.node("#error").textContent, "");
  assert.match(p.node("#toast").textContent, /修改申请已提交/);
  await assert.rejects(p.context.requestReceiptRevision("优化标题"), /不能申请修改/);
  assert.equal(p.requests.length, 2);
});

test("兑换前显示这张卡的冻结属性与修改额度，覆盖规格属性不会误用旧值", async () => {
  const p = page();
  const reading = p.context.readReceipt();
  p.requests[0].respond({ product: product("Revision product"), job: null, variant: { id: "plus", attributes: { edit_quota: 1 } }, card_attributes: { edit_quota: 3, paper_size: "A4", custom: "<script>unsafe<\/script>" }, entitlements: { ...entitlement(), total: 3, remaining: 3 } });
  await reading;
  const html = p.node("#app").innerHTML;
  assert.match(html, /修改机会 · 剩余 3 \/ 3/);
  assert.match(html, /paper_size/);
  assert.match(html, /&lt;script&gt;unsafe/);
  assert.doesNotMatch(html, /<script>unsafe/);
});

test("批量领取页面只为选定卡密提交修改建议", async () => {
  const p = page();
  const definition = product("Revision product");
  const batch = { batch: true, product: definition, items: [{ card_id: "card-one", product: definition, job: task() }, { card_id: "card-two", product: definition, job: task({ id: "other-job" }) }] };
  p.set({ currentBatch: batch, batchSelection: "card-one" });
  p.context.renderReceipt(task());
  const submitting = p.context.requestReceiptRevision("修改第一页");
  assert.equal(p.requests[0].body.card_id, "card-one");
  p.context.selectBatchCard("card-two");
  p.requests[0].respond(task({ state: "queued" }));
  await submitting;
  assert.equal(p.requests.length, 1);
  assert.equal(p.getContext().cardId, "card-two");
});
