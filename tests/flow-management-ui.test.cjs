const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush, parameter, product, job } = require("./app-fixture.cjs");
const fid = "11111111-1111-4111-8111-111111111111";
const flow = (epoch = 3, phase = "processing") => ({ enabled: true, flow_epoch: epoch, revision: 9, phase, current: { id: "step-" + epoch, kind: "process", label: { "zh-CN": "生成图片" } }, server_time: 1000, deadline: 1090, actions: [] });
const task = (id = "A", epoch = 3, extra = {}) => ({ ...job(id, "processing"), product_id: "product", params: {}, files: [], claimed_by: "owner", flow_epoch: epoch, action_id: "action-" + id, task_flow: flow(epoch), outputs: [parameter("content", "textarea")], ...extra });
async function queue(rows, definition = product(), values = {}) {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ tab: "jobs", role: "admin", ...values });
  const rendering = page.context.renderJobs("", "product");
  page.requests[0].respond([definition]);
  await flush();
  page.requests[1].respond(rows);
  await rendering;
  page.collections.set("[name=job]:checked", rows.map((row) => ({ value: row.id })));
  return page;
}
async function refresh(page, index) {
  await flush();
  page.requests[index].respond([product()]);
  await flush();
  page.requests[index + 1].respond([]);
}

test("queue views show the current step and deadline without invented percent or sensitive values", async () => {
  const row = task("A", 3, { state: "waiting", progress: 75, task_flow: flow(3, "input"), params: { topic: "文档需求", otp: "PRIVATE_OTP", unlisted_secret: "PRIVATE_OTHER" }, protected_fields: ["otp"], parameters: [parameter("topic"), { ...parameter("otp"), sensitive: true }, { ...parameter("unlisted_secret"), sensitive: true }] });
  const page = await queue([row]);
  const markup = page.node("#workspace").innerHTML;
  assert.match(markup, /等待顾客/);
  assert.match(markup, /生成图片/);
  assert.match(markup, /剩余约 90 秒/);
  assert.match(markup, /文档需求/);
  assert.doesNotMatch(markup, /PRIVATE_OTP|PRIVATE_OTHER|75%|undefined/);
});

test("only file IDs referenced by the current flow input appear in the management queue", async () => {
  const old = "22222222-2222-4222-8222-222222222222";
  const row = task("A", 3, { parameters: [parameter("picture", "image")], params: { picture: fid }, files: [
    { id: fid, kind: "input", filename: "current.png", size: 2000 },
    { id: old, kind: "input", filename: "old-step.png", size: 2000 },
  ] });
  const page = await queue([row]);
  assert.match(page.node("#workspace").innerHTML, /current.png/);
  assert.doesNotMatch(page.node("#workspace").innerHTML, /old-step.png/);
});

test("claiming tasks with different current epochs binds each job independently", async () => {
  const rows = [task("A", 3, { state: "queued" }), task("B", 8, { state: "queued" })];
  const page = await queue(rows);
  const claiming = page.node("#claim").emit("click");
  assert.deepEqual(page.requests[2].body.flow_scopes, {
    A: { flow_epoch: 3, action_id: "action-A", attempt: 1 },
    B: { flow_epoch: 8, action_id: "action-B", attempt: 1 },
  });
  page.requests[2].respond({ ok: true });
  await refresh(page, 3);
  await claiming;
});

test("a waiting input has no operator scope and cannot be claimed through a stale selection", async () => {
  const page = await queue([task("A", 3, { state: "waiting", flow_epoch: undefined, action_id: undefined, task_flow: flow(3, "input") })]);
  await page.node("#claim").emit("click");
  assert.equal(page.requests.length, 2);
  assert.match(page.node("#error").textContent, /没有可处理/);
});

test("shop operators may return their own claimed step for retry with its exact scope", async () => {
  const row = task("A", 3, { claimed_by: "shop:shop-one" });
  const page = await queue([row], product(), { authStatus: { role: "admin", shop_id: "shop-one" } });
  await page.node("#request-changes").emit("click");
  page.node("#disposition-reason").value = "外部服务暂时不可用";
  const returning = page.node("#disposition-submit").emit("click");
  assert.deepEqual(page.requests[2].body.flow_scopes.A, { flow_epoch: 3, action_id: "action-A", attempt: 1 });
  assert.equal(page.requests[2].body.action, "request_retry");
  page.requests[2].respond({ ok: true });
  await refresh(page, 3);
  await returning;
});

test("progress and completion keep the selected step scope instead of using a product default", async () => {
  for (const action of ["progress", "succeed"]) {
    const row = task();
    const page = await queue([row]);
    await page.node(action === "progress" ? "#progress-update" : "#complete").emit("click");
    page.node("#batch-progress").value = "25";
    page.node("#batch-output-0").value = "step result";
    const writing = page.node("#batch-submit").emit("click");
    assert.equal(page.requests[2].body.action, action);
    assert.deepEqual(page.requests[2].body.flow_scopes.A, { flow_epoch: 3, action_id: "action-A", attempt: 1 });
    page.requests[2].respond({ ok: true });
    await refresh(page, 3);
    await writing;
  }
});

test("refreshing file drafts cannot silently replace the selected process step", async () => {
  const row = task("A", 3, { outputs: [parameter("picture", "image")] });
  const page = await queue([row]);
  const opening = page.node("#complete").emit("click");
  page.requests[2].respond([task("A", 4, { outputs: row.outputs })]);
  await opening;
  assert.equal(page.node("#batch-form").innerHTML, "");
  assert.match(page.node("#error").textContent, /步骤已改变/);
});

test("management file uploads include exact epoch and action and recheck the step afterward", async () => {
  const row = task("A", 3, { outputs: [parameter("picture", "image")] });
  const page = await queue([row]);
  const uploading = page.context.uploadFile({ scope: "job", product_id: "product", job_id: "A", field_key: "picture", filename: "picture.png", base64: btoa("png"), flow_epoch: 3, action_id: "action-A", attempt: 1 });
  page.requests[2].respond([row]);
  await flush();
  await flush();
  assert.equal(page.requests[3].url, "/api/manage/files/upload");
  assert.equal(page.requests[3].body.flow_epoch, "3");
  assert.equal(page.requests[3].body.action_id, "action-A");
  page.requests[3].respond({ id: fid });
  await flush();
  assert.match(page.requests[4].url, /job_id=A/);
  page.requests[4].respond([row]);
  assert.equal((await uploading).id, fid);
});

test("a process transition during upload makes its returned attachment unusable to the caller", async () => {
  const row = task("A", 3, { outputs: [parameter("picture", "image")] });
  const page = await queue([row]);
  const uploading = page.context.uploadFile({ scope: "job", product_id: "product", job_id: "A", field_key: "picture", filename: "picture.png", base64: btoa("png"), flow_epoch: 3, action_id: "action-A", attempt: 1 });
  const rejected = assert.rejects(uploading, /步骤已改变/);
  page.requests[2].respond([row]);
  await flush();
  await flush();
  page.requests[3].respond({ id: fid });
  await flush();
  page.requests[4].respond([task("A", 4, { outputs: row.outputs })]);
  await rejected;
});

test("customer flow uploads use only current input fields and carry their compare-and-set scope", async () => {
  const page = appFixture();
  const input = { ...flow(3, "input"), current: { id: "input-step", fields: [parameter("picture", "image")] }, actions: ["answer"] };
  page.set({ currentToken: "batch-token", currentProduct: product("Flow", [parameter("old_file", "file")]), currentJob: { ...job("A", "waiting"), task_flow: input } });
  const bad = page.context.uploadFile({ scope: "customer", field_key: "old_file", filename: "old.txt", base64: btoa("old"), flow_epoch: 3, expected_revision: 9, node_id: "input-step" });
  await assert.rejects(bad, /当前步骤没有/);
  assert.equal(page.requests.length, 0);
  const uploading = page.context.uploadFile({ scope: "customer", field_key: "picture", filename: "picture.png", base64: btoa("png"), flow_epoch: 3, expected_revision: 9, node_id: "input-step" });
  await flush();
  assert.equal(page.requests[0].body.flow_epoch, "3");
  assert.equal(page.requests[0].body.expected_revision, "9");
  assert.equal(page.requests[0].body.node_id, "input-step");
  page.requests[0].respond({ id: fid });
  assert.equal((await uploading).id, fid);
});

test("native flow operations bind receipt, epoch and revision and ignore a delayed previous-step result", async () => {
  const page = appFixture();
  const input = { ...flow(3, "input"), actions: ["answer"] };
  page.set({ currentToken: "batch-token", currentProduct: product(), currentJob: { ...job("A", "waiting"), task_flow: input } });
  const answering = page.context.flowAction("answer", { otp: "PRIVATE_INPUT" }, { flow_epoch: 3, expected_revision: 9 });
  assert.deepEqual(page.requests[0].body, { token: "batch-token", flow_epoch: 3, expected_revision: 9, values: { otp: "PRIVATE_INPUT" } });
  page.set({ currentJob: { ...job("A", "waiting"), task_flow: flow(4, "input") } });
  page.node("#app").innerHTML = "New step";
  page.requests[0].respond({ ...job("A", "processing"), task_flow: flow(3) });
  await answering;
  assert.equal(page.node("#app").innerHTML, "New step");
  await assert.rejects(page.context.flowAction("start", {}, { flow_epoch: 3, expected_revision: 9 }), /不支持|已改变/);
});


test("DOM-only redemption reads the page input privately and keeps navigation bound to its original page", async () => {
  const page = appFixture();
  page.navigate("/");
  page.node("#code").value = "OPAQUE_PAGE_ONLY_CODE";
  const exchanging = page.getActions().exchangePasted({});
  assert.equal(page.requests[0].body.code, "OPAQUE_PAGE_ONLY_CODE");
  page.navigate("/admin");
  page.node("#app").innerHTML = "Management page";
  page.requests[0].respond({ token: "private-receipt", product: product(), job: null });
  await exchanging;
  assert.equal(page.node("#app").innerHTML, "Management page");
  await assert.rejects(page.getActions().exchangePasted({}), /兑换页/);
});


test("an ended failed flow can still be released after checking external fulfillment", async () => {
  const row = task("A", 3, { state: "failed", attempt: 2, action_id: undefined, flow_epoch: undefined, task_flow: flow(3, "ended") });
  const page = await queue([row]);
  const releasing = page.node("#release").emit("click");
  assert.equal(page.requests[2].body.action, "retry");
  assert.deepEqual(page.requests[2].body.flow_scopes, { A: { attempt: 2 } });
  page.requests[2].respond({ ok: true });
  await refresh(page, 3);
  await releasing;
});
