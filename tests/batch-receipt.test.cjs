const assert = require("node:assert/strict");
const test = require("node:test");
const { File } = require("node:buffer");
const { appFixture, flush, parameter, product, job, item, batch } = require("./app-fixture.cjs");

test("a delayed reveal cannot populate another card, even after returning to the original card", async () => {
  for (const returnToOriginal of [false, true]) {
    const page = appFixture();
    const a = item("A", product("A"));
    const b = item("B", product("B"));
    batch(page, [a, b]);
    page.context.openBatchCard(a);
    const revealing = page.context.revealReceipt();
    assert.equal(page.requests[0].body.card_id, "A");
    page.context.openBatchCard(b);
    if (returnToOriginal) page.context.openBatchCard(a);
    page.node("#content").innerHTML = "Current card's content";
    page.requests[0].respond({ output: { content: "Late delivery for A" } });
    await revealing;
    assert.equal(page.node("#content").innerHTML, "Current card's content");
  }
});

test("receipt reads, submissions, and destruction remain bound to the selected card", async () => {
  for (const action of ["readReceipt", "submitRedemption", "destroyReceipt"]) {
    const page = appFixture();
    const a = item("A", product("A"));
    const b = item("B", product("B"));
    const data = batch(page, [a, b]);
    page.context.openBatchCard(a);
    const pending = action === "submitRedemption"
      ? page.context[action]([{ card_id: "A", params: {} }])
      : page.context[action]();
    if (action === "destroyReceipt") assert.equal(page.requests[0].body.card_id, "A");
    page.context.openBatchCard(b);
    page.node("#content").innerHTML = "B delivery";
    const markup = page.node("#app").innerHTML;
    page.requests[0].respond(action === "destroyReceipt" ? { ok: true } : data);
    await pending;
    assert.equal(page.node("#app").innerHTML, markup, action);
    assert.equal(page.node("#content").innerHTML, "B delivery", action);
    assert.notEqual(page.node("#reveal").removed, true, action);
    assert.equal(page.requests.length, 1, action);
  }
});

test("entering retry on the same card invalidates earlier receipt responses", async () => {
  const page = appFixture();
  const a = item("A", product("A", [parameter("old_field")]), job("A", "failed"));
  const data = batch(page, [a]);
  page.context.openBatchCard(a);
  const reading = page.context.readReceipt();
  await page.node("#retry").emit("click");
  const markup = page.node("#app").innerHTML;
  page.requests[0].respond(data);
  await reading;
  assert.equal(page.node("#app").innerHTML, markup);
  assert.match(markup, /param-A-old_field/);
});

test("each card renders and submits its own parameter snapshot, including file fields", async () => {
  const page = appFixture();
  const a = item("A", product("Old definition", [parameter("old_text"), parameter("old_file", "file")]), null);
  const b = item("B", product("Current definition", [parameter("new_email", "email")]), null);
  const data = batch(page, [a, b]);
  page.context.openBatch(data);
  const markup = page.node("#app").innerHTML;
  assert.match(markup, /param-A-old_text/);
  assert.match(markup, /param-A-old_file/);
  assert.match(markup, /param-B-new_email/);
  assert.doesNotMatch(markup, /param-B-old_text|param-B-old_file|param-A-new_email/);
  page.node("#param-A-old_text").value = "old value";
  page.node("#param-A-old_file").files = [new File(["input"], "input.txt")];
  page.node("#param-B-new_email").value = "b@example.test";
  const submitting = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests[0].url, "/api/files/upload");
  assert.equal(page.requests[0].body.card_id, "A");
  assert.equal(page.requests[0].body.field_key, "old_file");
  page.requests[0].respond({ id: "file-A" });
  await flush();
  assert.equal(page.requests[1].url, "/api/redeem");
  assert.deepEqual(page.requests[1].body.items, [
    { card_id: "A", params: { old_text: "old value", old_file: "file-A" } },
    { card_id: "B", params: { new_email: "b@example.test" } },
  ]);
  page.requests[1].respond({ ...data, items: [a, b].map((row) => ({ ...row, job: job(row.card_id, "queued") })) });
  await submitting;
});

test("copying parameters skips files, absent keys, and fields with different types", async () => {
  const page = appFixture();
  const a = item("A", product("A", [parameter("shared"), parameter("different"), parameter("only_a"), parameter("file", "file")]), null);
  const b = item("B", product("B", [parameter("shared"), parameter("different", "number"), parameter("file", "file")]), null);
  page.context.openBatch(batch(page, [a, b]));
  page.node("#param-A-shared").value = "copied";
  page.node("#param-A-different").value = "wrong type";
  page.node("#param-B-different").value = "42";
  page.node("#param-B-file").value = "existing file";
  await page.node("#copy-params").emit("click");
  assert.equal(page.node("#param-B-shared").value, "copied");
  assert.equal(page.node("#param-B-different").value, "42");
  assert.equal(page.node("#param-B-file").value, "existing file");
});

test("delivery labels and receipt refresh use the selected card's output snapshot", async () => {
  const page = appFixture();
  const definition = product("Selected product");
  definition.outputs = [{ ...parameter("account"), label: { "zh-CN": "原任务账号" } }];
  const a = item("A", definition);
  const b = item("B", product("Other product"));
  const data = batch(page, [a, b]);
  page.context.openBatchCard(a);
  const reading = page.context.readReceipt();
  page.requests[0].respond(data);
  await reading;
  assert.equal(page.getContext().product.name, "Selected product");
  const revealing = page.context.revealReceipt();
  page.requests[1].respond({ output: { account: "buyer@example.test" } });
  await revealing;
  assert.match(page.node("#content").innerHTML, /<h3>原任务账号<\/h3>/);
});

test("delivery file downloads capture the card and ignore a late blob after switching", async () => {
  const page = appFixture();
  const a = item("A", product("A"));
  const b = item("B", product("B"));
  batch(page, [a, b]);
  page.context.openBatchCard(a);
  const downloading = page.context.downloadDeliveryFile({ id: "file-A", filename: "a.txt" });
  assert.equal(page.requests[0].body.card_id, "A");
  page.context.openBatchCard(b);
  page.requests[0].respond({});
  await downloading;
  assert.equal(page.downloads.length, 0);
});
