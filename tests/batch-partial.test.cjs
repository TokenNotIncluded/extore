const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { File } = require("node:buffer");
const { appFixture, flush, parameter, product, job, item } = require("./app-fixture.cjs");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/batch-redemption.js"), "utf8");
const ready = (id, definition, variant = "") => ({
  ...item(id, definition, null), index: id === "A" ? 0 : 1,
  accepted: true, status: "valid", error: null,
  variant: variant ? { id: variant, name: variant } : null,
});
const dataOf = (items) => ({ batch: true, partial: true, items });
function setup(items) {
  const page = appFixture();
  vm.runInContext(source, page.context);
  const data = dataOf(items);
  page.set({ currentToken: "batch-token", currentBatch: data, currentProduct: items[0]?.product || null });
  return { ...page, data, module: page.context.window.ExtoreBatchRedemption };
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const submitted = (data) => ({ ...data, items: data.items.map((row) => row.accepted
  ? { ...row, job: job(row.card_id, "queued"), status: "used" } : row) });

test("only compatible frozen fields share input; product and variant grouping stay separate", () => {
  const definition = product("One", [parameter("requirements"), parameter("attachment", "file")]);
  const other = { ...definition, id: "other", name: "Two" };
  const page = setup([ready("A", definition, "Word"), ready("B", definition, "PPT"), ready("C", other, "Word")]);
  const groups = page.module.groupItems(page.data.items);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].groups.length, 1);
  assert.deepEqual(plain(groups[0].groups[0].variants.map((variant) => variant.name)), ["Word", "PPT"]);
  page.context.openBatch(page.data);
  const markup = page.node("#app").innerHTML;
  assert.equal((markup.match(/id="batch-param-0-requirements"/g) || []).length, 1);
  assert.match(markup, /id="batch-param-1-requirements"/);
  assert.match(markup, /batch-file-A-attachment/);
  assert.match(markup, /batch-file-B-attachment/);
  assert.match(markup, /batch-file-C-attachment/);
});

test("matching parameter names never merge a changed tutorial, validation or saved requirement", () => {
  const field = parameter("requirements");
  const base = product("One", [field]);
  const changed = { ...base, parameters: [{ ...field, description: { "zh-CN": "Another requirement" } }] };
  const optional = { ...base, parameters: [{ ...field, required: false }] };
  const reordered = { ...base, parameters: [Object.fromEntries(Object.entries(field).reverse())] };
  const page = setup([ready("A", base), ready("B", changed), ready("C", optional), ready("D", reordered)]);
  const groups = page.module.groupItems(page.data.items)[0].groups;
  assert.equal(groups.length, 3);
  assert.deepEqual(plain(groups[0].items.map((card) => card.card_id)), ["A", "D"]);
  const drafts = new Map([["A", { requirements: "Keep my first brief" }], ["D", { requirements: "Different brief" }]]);
  assert.equal(page.module.groupItems(page.data.items, { drafts })[0].groups.length, 4);
});

test("multi-code verification previews bad and duplicate entries without submitting or rendering raw codes", async () => {
  const page = setup([]);
  page.navigate("/");
  const raw = "FIRST-PRIVATE-CODE\nINVALID-PRIVATE-CODE\nFIRST-PRIVATE-CODE";
  const exchanging = page.context.exchangeCode(raw);
  assert.equal(page.requests[0].url, "/api/batch/exchange");
  const response = { ...dataOf([
    ready("A", product("Product", [parameter("requirements")])),
    { index: 1, suffix: "E-CODE", accepted: false, status: "invalid", error: "卡密无效" },
    { index: 2, suffix: "E-CODE", accepted: false, status: "duplicate", error: "卡密重复输入" },
  ]), token: "safe-receipt-token" };
  page.requests[0].respond(response);
  await exchanging;
  const markup = page.node("#app").innerHTML;
  assert.match(markup, /卡密无效/);
  assert.match(markup, /重复，已跳过/);
  assert.doesNotMatch(markup, /FIRST-PRIVATE-CODE|INVALID-PRIVATE-CODE/);
  assert.equal(page.requests.length, 1);
  assert.equal(page.context.location.hash, "#safe-receipt-token");
});

test("all-invalid input has no receipt link or misleading navigation", async () => {
  const page = setup([]);
  page.navigate("/");
  const exchange = page.context.exchangeCode("INVALID-A\nINVALID-B");
  page.requests[0].respond({ ...dataOf([
    { index: 0, suffix: "A", accepted: false, status: "invalid", error: "卡密无效" },
    { index: 1, suffix: "B", accepted: false, status: "invalid", error: "卡密无效" },
  ]), token: null });
  await exchange;
  assert.equal(page.context.location.pathname, "/");
  assert.equal(page.context.location.hash, "");
  assert.doesNotMatch(page.node("#app").innerHTML, /\/receipt#|id="form"/);
  assert.match(page.node("#app").innerHTML, /卡密无效/);
});

test("shared text is submitted to both variants while each upload remains bound to its own card", async () => {
  const definition = product("Document", [parameter("requirements"), parameter("attachment", "file")]);
  const page = setup([ready("A", definition, "Word"), ready("B", definition, "PPT")]);
  page.context.openBatch(page.data);
  for (const id of ["A", "B"]) page.node(`#batch-select-${id}`).checked = true;
  page.node("#batch-param-0-requirements").value = "One brief, two formats";
  const sameReference = new File(["Shared source material"], "reference.txt");
  page.node("#batch-file-A-attachment").files = [sameReference];
  page.node("#batch-file-B-attachment").files = [sameReference];
  const saving = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests[0].url, "/api/files/upload");
  assert.equal(page.requests[0].body.card_id, "A");
  page.requests[0].respond({ id: "file-A" });
  await flush();
  assert.equal(page.requests[1].body.card_id, "B");
  page.requests[1].respond({ id: "file-B" });
  await flush();
  assert.equal(page.requests[2].url, "/api/batch/redeem");
  assert.deepEqual(page.requests[2].body.items, [
    { card_id: "A", params: { requirements: "One brief, two formats", attachment: "file-A" } },
    { card_id: "B", params: { requirements: "One brief, two formats", attachment: "file-B" } },
  ]);
  page.requests[2].respond(submitted(page.data));
  await saving;
});

test("unchecked cards and their required fields do not block another product", async () => {
  const a = ready("A", product("First", [parameter("requirements")]));
  const b = ready("B", { ...product("Second", [parameter("requirements")]), id: "second" });
  const page = setup([a, b]);
  page.context.openBatch(page.data);
  page.node("#batch-select-A").checked = true;
  page.node("#batch-select-B").checked = false;
  page.node("#batch-param-0-requirements").value = "Only for first product";
  page.node("#batch-param-1-requirements").reportValidity = () => { throw new Error("Must not validate unchecked product"); };
  const saving = page.node("#form").emit("submit");
  await flush();
  assert.deepEqual(page.requests[0].body.items, [{ card_id: "A", params: { requirements: "Only for first product" } }]);
  page.requests[0].respond({ ...page.data, items: [{ ...a, job: job("A", "queued") }, b] });
  await saving;
});

test("one failed upload does not discard another card and keeps the failed card's brief", async () => {
  const definition = product("Document", [parameter("requirements"), parameter("attachment", "file")]);
  const page = setup([ready("A", definition), ready("B", definition)]);
  page.context.openBatch(page.data);
  for (const id of ["A", "B"]) page.node(`#batch-select-${id}`).checked = true;
  page.node("#batch-param-0-requirements").value = "Keep this brief after a partial failure";
  page.node("#batch-file-A-attachment").files = [new File(["A"], "a.txt")];
  page.node("#batch-file-B-attachment").files = [new File(["B"], "b.txt")];
  const saving = page.node("#form").emit("submit");
  await flush();
  page.requests[0].respond({ id: "file-A" });
  await flush();
  page.requests[1].reject(new Error("B upload failed"));
  await flush();
  assert.equal(page.requests[2].url, "/api/batch/redeem");
  assert.deepEqual(page.requests[2].body.items, [{ card_id: "A", params: {
    requirements: "Keep this brief after a partial failure", attachment: "file-A",
  } }]);
  page.requests[2].respond({ ...page.data, items: [{ ...page.data.items[0], job: job("A", "queued") }, page.data.items[1]] });
  await saving;
  assert.match(page.node("#app").innerHTML, /Keep this brief after a partial failure/);
  assert.match(page.node("#app").innerHTML, /B upload failed/);
});

test("a page switch after upload prevents submitting a stale batch", async () => {
  const page = setup([ready("A", product("One", [parameter("attachment", "file")]))]);
  page.context.openBatch(page.data);
  page.node("#batch-select-A").checked = true;
  page.node("#batch-file-A-attachment").files = [new File(["A"], "a.txt")];
  const saving = page.node("#form").emit("submit");
  await flush();
  page.navigate("/");
  page.requests[0].respond({ id: "file-A" });
  await saving;
  assert.equal(page.requests.length, 1);
});

test("a server-rejected card reuses its own uploaded file instead of consuming quota again", async () => {
  const page = setup([ready("A", product("One", [parameter("requirements"), parameter("attachment", "file")]))]);
  page.context.openBatch(page.data);
  page.node("#batch-select-A").checked = true;
  page.node("#batch-param-0-requirements").value = "Keep my brief";
  page.node("#batch-file-A-attachment").files = [new File(["Input"], "input.txt")];
  const saving = page.node("#form").emit("submit");
  await flush();
  page.requests[0].respond({ id: "own-file-A" });
  await flush();
  page.requests[1].respond({ ...page.data, results: [{ card_id: "A", status: "error", error: "Temporary rejection" }] });
  await saving;
  assert.match(page.node("#app").innerHTML, /Keep my brief/);
  assert.match(page.node("#app").innerHTML, /value="own-file-A"/);
  page.node("#batch-file-A-attachment").files = [];
  const retrying = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests[2].url, "/api/batch/redeem");
  assert.deepEqual(page.requests[2].body.items, [{ card_id: "A", params: { requirements: "Keep my brief", attachment: "own-file-A" } }]);
  page.requests[2].respond(submitted(page.data));
  await retrying;
  assert.equal(page.requests.length, 3);
});

test("flow preparation sends empty params and never starts another card or exposes future questions", async () => {
  const definition = product("Flow", [parameter("future_question")]);
  definition.task_flow_view = { enabled: true, definition_hash: "hash", phase: "await_start", actions: ["start"] };
  const page = setup([ready("A", definition), ready("B", definition)]);
  page.context.openBatch(page.data);
  assert.doesNotMatch(page.node("#app").innerHTML, /future_question/);
  assert.equal(page.requests.length, 0);
  for (const id of ["A", "B"]) page.node(`#batch-select-${id}`).checked = true;
  const preparing = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests[0].url, "/api/batch/redeem");
  assert.deepEqual(page.requests[0].body.items, [{ card_id: "A", params: {} }, { card_id: "B", params: {} }]);
  const waiting = { ...page.data, items: page.data.items.map((row) => ({ ...row, job: {
    ...job(row.card_id, "waiting"), task_flow: {
      enabled: true, phase: "await_start", flow_epoch: 1, revision: 1, actions: ["start"],
    },
  } })) };
  page.requests[0].respond(waiting);
  await preparing;
  assert.equal(page.requests.length, 1);
  const starting = page.node('[data-batch-start="A"]').emit("click");
  await flush();
  assert.equal(page.requests[1].url, "/api/task-flow/start");
  assert.deepEqual(page.requests[1].body, { token: "batch-token", card_id: "A", flow_epoch: 1, expected_revision: 1 });
  page.requests[1].respond({ ...waiting.items[0].job, task_flow: { enabled: true, phase: "question", actions: ["answer"] } });
  await flush();
  assert.equal(page.requests[2].url, "/api/batch/receipt");
  page.requests[2].respond(waiting);
  await starting;
  assert.equal(page.requests.length, 3);
  assert.equal(page.getContext().cardId, "A");
});

test("opening one unprepared flow card still shows only its preparation, never its future fields", async () => {
  const definition = product("Flow", [parameter("future_question")]);
  definition.task_flow_view = { enabled: true, definition_hash: "hash", phase: "await_start", actions: ["start"] };
  const page = setup([ready("A", definition), ready("B", definition)]);
  page.context.renderBatch(page.data);
  await page.node('[data-batch-open="B"]').emit("click");
  assert.doesNotMatch(page.node("#app").innerHTML, /future_question|batch-select-A/);
  assert.match(page.node("#app").innerHTML, /batch-select-B/);
  assert.equal(page.requests.length, 0);
  page.node("#batch-select-B").checked = true;
  const preparing = page.node("#form").emit("submit");
  await flush();
  assert.deepEqual(page.requests[0].body.items, [{ card_id: "B", params: {} }]);
  assert.equal(page.requests[0].url, "/api/batch/redeem");
  page.requests[0].respond({ ...page.data, items: [page.data.items[0], { ...page.data.items[1], job: job("B", "waiting") }] });
  await preparing;
  assert.equal(page.requests.length, 1);
});

test("receipt reload falls back for a legacy single token only on HTTP 404", async () => {
  for (const status of [404, 500]) {
    const page = setup([]);
    page.set({ currentBatch: null });
    const calls = [];
    const original = page.context.fetch;
    page.context.fetch = (url, options) => {
      calls.push(url);
      if (url === "/api/batch/receipt") return Promise.resolve({ ok: false, status, json: async () => ({ detail: "Not a batch" }) });
      return original(url, options);
    };
    const reading = page.context.readReceipt();
    await flush();
    if (status === 404) {
      assert.deepEqual(calls, ["/api/batch/receipt", "/api/receipt"]);
      page.requests[0].respond({ product: product("Single"), job: job("single") });
      await reading;
    } else {
      await assert.rejects(reading, (error) => error.status === 500);
      assert.deepEqual(calls, ["/api/batch/receipt"]);
    }
  }
});


test("mixed variant image sets stay per card and keep their ordered references", async () => {
  const definition = product("Image service", [parameter("requirements"), { ...parameter("gallery", "images"), max_items: 2 }]);
  const page = setup([ready("A", definition, "Basic"), ready("B", definition, "Plus")]);
  page.context.openBatch(page.data);
  const markup = page.node("#app").innerHTML;
  assert.doesNotMatch(markup, /batch-param-0-gallery/);
  assert.match(markup, /batch-file-A-gallery/);
  assert.match(markup, /batch-file-B-gallery/);
  page.node("#batch-param-0-requirements").value = "Same brief";
  const images = [new File(["a"], "a.png", { type: "image/png" }), new File(["b"], "b.png", { type: "image/png" })];
  for (const id of ["A", "B"]) {
    page.node(`#batch-select-${id}`).checked = true;
    page.node(`#batch-file-${id}-gallery`).files = images;
  }
  const saving = page.node("#form").emit("submit");
  for (const [index, id] of ["A", "A", "B", "B"].entries()) {
    await flush();
    assert.equal(page.requests[index].body.card_id, id);
    assert.equal(page.requests[index].body.field_key, "gallery");
    page.requests[index].respond({ id: `image-${id}-${index % 2}` });
  }
  await flush();
  assert.deepEqual(page.requests[4].body.items, [
    { card_id: "A", params: { requirements: "Same brief", gallery: '["image-A-0","image-A-1"]' } },
    { card_id: "B", params: { requirements: "Same brief", gallery: '["image-B-0","image-B-1"]' } },
  ]);
  page.requests[4].respond(submitted(page.data));
  await saving;
});
