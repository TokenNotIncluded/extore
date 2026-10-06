const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush, product, job } = require("./app-fixture.cjs");

function queue() {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ tab: "jobs", role: "admin" });
  return page;
}
async function load(page, options = {}) {
  const offset = page.requests.length;
  const pending = page.context.renderJobs(options.state || "", options.productId || "A", options.view);
  page.requests[offset].respond([{ ...product("A"), id: "A" }, { ...product("B"), id: "B" }]);
  await flush();
  const request = page.requests[offset + 1];
  assert.equal(request.url.split("?")[0], "/api/manage/jobs");
  request.respond(options.rows || []);
  await pending;
  return new URL("https://example.test" + request.url).searchParams;
}

test("queues default to active and expose a processed/all view without dropping state filters", async () => {
  const page = queue();
  const initial = await load(page);
  assert.equal(initial.get("view"), "active");
  assert.match(page.node("#workspace").innerHTML, /id="job-view"/);
  assert.match(page.node("#workspace").innerHTML, /value="active" selected>待处理/);
  const filtered = await load(page, { state: "destroyed", view: "processed" });
  assert.equal(filtered.get("view"), "processed");
  assert.equal(filtered.get("state"), "destroyed");
  assert.equal(page.getContext().queueView, "processed");
  const all = await load(page, { view: "all" });
  assert.equal(all.get("view"), "all");
});

test("changing queue views clears old selections and batch forms before responses arrive", async () => {
  const page = queue();
  await load(page);
  const selected = { checked: true };
  page.collections.set("[name=job]", [selected]);
  page.node("#all").checked = true;
  page.node("#batch-form").innerHTML = "Old batch confirmation";
  const changing = page.context.renderJobs("", "B", "processed");
  assert.equal(selected.checked, false);
  assert.equal(page.node("#all").checked, false);
  assert.equal(page.node("#batch-form").innerHTML, "");
  page.requests[2].respond([{ ...product("B"), id: "B" }]);
  await flush();
  assert.match(page.requests[3].url, /view=processed/);
  page.requests[3].respond([]);
  await changing;
});

test("refresh and successful batch mutations preserve the selected view and explicit state", async () => {
  const page = queue();
  const row = { ...job("job-A", "queued"), product_id: "A", params: {}, files: [] };
  await load(page, { state: "queued", view: "all", rows: [row] });
  page.collections.set("[name=job]:checked", [{ value: row.id }]);
  const claiming = page.node("#claim").emit("click");
  assert.equal(page.requests[2].url, "/api/manage/batch");
  page.requests[2].respond({ ok: true });
  await flush();
  page.requests[3].respond([{ ...product("A"), id: "A" }]);
  await flush();
  const query = new URL("https://example.test" + page.requests[4].url).searchParams;
  assert.equal(query.get("view"), "all");
  assert.equal(query.get("state"), "queued");
  page.requests[4].respond([]);
  await claiming;
  const refreshing = page.node("#refresh").emit("click");
  page.requests[5].respond([{ ...product("A"), id: "A" }]);
  await flush();
  assert.match(page.requests[6].url, /view=all/);
  assert.match(page.requests[6].url, /state=queued/);
  page.requests[6].respond([]);
  await refreshing;
});

test("an earlier batch completion cannot reopen a different product or view", async () => {
  const page = queue();
  await load(page, { rows: [{ ...job("job-A", "queued"), product_id: "A", params: {}, files: [] }] });
  page.collections.set("[name=job]:checked", [{ value: "job-A" }]);
  const claiming = page.node("#claim").emit("click");
  await load(page, { productId: "B", view: "processed" });
  const count = page.requests.length;
  const markup = page.node("#workspace").innerHTML;
  page.requests[2].respond({ ok: true });
  await claiming;
  assert.equal(page.requests.length, count);
  assert.equal(page.getContext().queueProductId, "B");
  assert.equal(page.getContext().queueView, "processed");
  assert.equal(page.node("#workspace").innerHTML, markup);
});
