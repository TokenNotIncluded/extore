const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { appFixture, flush } = require("./app-fixture.cjs");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/progress-board.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../extore/static/progress-board.css"), "utf8");
const identitySource = fs.readFileSync(path.join(__dirname, "../extore/static/worker-identity.js"), "utf8");
const ids = { shop: "11111111-1111-4111-8111-111111111111", otherShop: "22222222-2222-4222-8222-222222222222", product: "33333333-3333-4333-8333-333333333333", otherProduct: "44444444-4444-4444-8444-444444444444", job: "55555555-5555-4555-8555-555555555555", worker: "a".repeat(64) };
const stateCounts = () => ({ waiting: 0, queued: 0, processing: 1, succeeded: 2, failed: 0, needs_input: 0, rejected: 1, destroyed: 1 });
function board(overrides = {}) {
  return { schema: "extore.progress-board.v1", generated_at: 1800000600, shop: { id: ids.shop, name: "Test shop" }, totals: stateCounts(),
    products: [{ id: ids.product, name: "Document pipeline", mode: "manual", counts: stateCounts(), jobs: [{ id: ids.job, state: "processing", progress: 37, attempt: 1, created: 1800000000, updated: 1800000500, queue_position: null, worker_id: ids.worker, step_count: 3, completed_step_count: 1, steps: [{ position: 1, state: "done" }, { position: 2, state: "current" }, { position: 3, state: "pending" }], flow_phase: "processing" }] }],
    workers: [{ id: ids.worker, name: "Document Bot", kind: "unknown", active_jobs: 1, completed_jobs: 4, last_update: 1800000500 }],
    pagination: { limit: 100, offset: 0, total: 1, has_more: false }, scope: { product_ids: [ids.product] }, ...overrides };
}
function fixture(options = {}) {
  const nodes = new Map(), requests = [], timers = new Map(), docListeners = new Map(), winListeners = new Map(), copies = [], filters = [];
  let current = true, timerId = 0;
  function element() {
    const listeners = new Map();
    let html = "";
    return { isConnected: true, value: "", textContent: "", disabled: false, attributes: {},
      set innerHTML(value) { html = value; for (const match of String(value).matchAll(/\bid="([^"]+)"([^>]*)/g)) { const child = nodes.get("#" + match[1]) || element(); child.disabled = /\bdisabled\b/.test(match[2]); nodes.set("#" + match[1], child); } },
      get innerHTML() { return html; },
      querySelector: (selector) => nodes.get(selector) || null,
      addEventListener(name, callback) { listeners.set(name, callback); },
      setAttribute(name, value) { this.attributes[name] = value; },
      async emit(name) { await listeners.get(name)?.({ target: this }); await flush(); },
    };
  }
  const root = element();
  const document = { hidden: false, addEventListener: (name, callback) => docListeners.set(name, callback), removeEventListener: (name, callback) => { if (docListeners.get(name) === callback) docListeners.delete(name); } };
  const window = { addEventListener: (name, callback) => winListeners.set(name, callback), removeEventListener: (name, callback) => { if (winListeners.get(name) === callback) winListeners.delete(name); } };
  const context = vm.createContext({ window, document, URLSearchParams, AbortController, setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; }, clearTimeout: (id) => timers.delete(id) });
  if (options.workerImages) vm.runInContext(identitySource, context);
  vm.runInContext(source, context);
  const ctx = { root, auth: { role: "staff", shop_id: ids.shop, product_id: ids.product, permissions: ["queue.monitor"] }, language: "zh-CN", isCurrent: () => current,
    api: (path, body, method, settings) => new Promise((resolve, reject) => requests.push({ path, body, method, settings, respond: resolve, reject })),
    copyPrompt: async (...args) => copies.push(args), onFilters: (value) => filters.push(value), ...options };
  const controller = window.ExtoreProgressBoard.mount(ctx);
  const tick = async () => { const [id, value] = timers.entries().next().value || []; if (!value) return; timers.delete(id); value.callback(); await flush(); };
  return { root, nodes, requests, timers, document, docListeners, winListeners, controller, copies, filters, tick, node: (selector) => nodes.get(selector), current: (value) => { current = value; }, async hidden(value) { document.hidden = value; docListeners.get("visibilitychange")?.(); await flush(); } };
}
test("monitor-only board uses its dedicated GET, safe projection and actual progress", async () => {
  const page = fixture();
  assert.match(page.requests[0].path, /^\/manage\/progress-board\?/);
  assert.equal(page.requests[0].method, "GET");
  assert.match(page.requests[0].path, /product_id=/);
  const data = board();
  data.products[0].name = "<script>bad</script>";
  data.workers[0].name = '<img src=x onerror="evil()">';
  Object.assign(data.products[0].jobs[0], { params: { prompt: "PRIVATE_REQUEST" }, message: "PRIVATE_MESSAGE", output: "PRIVATE_OUTPUT", attachments: ["PRIVATE_ATTACHMENT"] });
  data.products[0].jobs[0].steps[1].label = "PRIVATE_CUSTOM_STEP";
  page.requests[0].respond(data); await page.controller.ready;
  const html = page.node("#board-data").innerHTML;
  assert.match(html, /value="37"/); assert.match(html, /37%/);
  assert.match(html, /当前环节上报进度/);
  assert.match(html, /不代表整单完成比例/);
  assert.match(html, /已完成 1 \/ 3 步/); assert.match(html, /步骤 2：当前步骤/);
  assert.match(html, /&lt;script&gt;bad&lt;\/script&gt;/); assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /PRIVATE_|<script|<img|href=|data-job|onclick=/);
  assert.match(html, /已处理/); assert.doesNotMatch(html, /在线|预计|ETA/);
  assert.equal(page.timers.size, 1); assert.equal([...page.timers.values()][0].delay, 5000);
});
test("real TestClient progress-board contract renders without DTO aliases", async () => {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/progress-board-contract.json"), "utf8"));
  const page = fixture({ auth: { role: "admin", shop_id: data.shop.id } });
  page.requests[0].respond(data); await page.controller.ready;
  assert.match(page.node("#board-data").innerHTML, /可见商品|可见处理人员/);
  assert.match(page.node("#board-data").innerHTML, /商品内排第 1 位/);
  assert.match(page.node("#board-data").innerHTML, /已完成 1 \/ 2 步/);
  assert.equal(page.timers.size, 1); assert.doesNotMatch(page.node("#board-status").textContent, /不可用/);
});
test("CLI worker cartoons follow actual snapshot metadata and custom types cannot inject an avatar URL", async () => {
  const page = fixture({ workerImages: true }), data = board();
  data.workers[0].kind = "cli"; data.workers[0].agent_type = "grok-bot";
  page.requests[0].respond(data); await page.controller.ready;
  const html = page.node("#board-data").innerHTML;
  assert.match(html, /src="\/static\/worker-avatars\/grok-bot\.webp"/);
  assert.match(html, /自报类型：grok-bot · 未验证/);
  assert.doesNotMatch(html, /https:\/\//);
});
test("root must choose a named shop; private shop DTO fields are not rendered", async () => {
  const page = fixture({ auth: { role: "admin", shop_id: null, superadmin: true }, platform: true });
  assert.equal(page.requests[0].path, "/platform/shops");
  page.requests[0].respond([{ id: ids.shop, name: "lightstore", email: "PRIVATE_EMAIL" }, { id: ids.otherShop, name: "Second shop" }]);
  await page.controller.ready;
  assert.equal(page.requests.length, 1); assert.equal(page.timers.size, 0);
  assert.match(page.node("#board-shop").innerHTML, /lightstore/);
  assert.doesNotMatch(page.node("#board-shop").innerHTML, /PRIVATE_EMAIL/);
  page.node("#board-shop").value = ids.shop; await page.node("#board-shop").emit("change");
  assert.match(page.requests[1].path, new RegExp("shop_id=" + ids.shop));
  assert.doesNotMatch(page.requests[1].path, /product_id=/);
  page.requests[1].respond(board()); await flush();
  page.node("#board-shop").value = ids.otherShop; await page.node("#board-shop").emit("change");
  assert.equal(page.node("#board-data").innerHTML, "");
  assert.match(page.requests[2].path, new RegExp("shop_id=" + ids.otherShop));
  page.requests[2].respond(board()); await flush();
  assert.equal(page.node("#board-data").innerHTML, "");
  assert.match(page.node("#board-status").textContent, /当前进度不可用/);
});
test("root shop discovery resumes after a hidden-page abort without fetching any board", async () => {
  const page = fixture({ auth: { role: "admin", shop_id: null, superadmin: true }, platform: true });
  await page.hidden(true); assert.equal(page.requests[0].settings.signal.aborted, true);
  page.requests[0].respond([{ id: ids.shop, name: "Stale shop" }]); await page.controller.ready;
  assert.doesNotMatch(page.node("#board-shop").innerHTML, /Stale shop/);
  await page.hidden(false); assert.equal(page.requests[1].path, "/platform/shops");
  page.requests[1].respond([{ id: ids.shop, name: "Current shop" }]); await flush();
  assert.match(page.node("#board-shop").innerHTML, /Current shop/);
  assert.equal(page.requests.length, 2); assert.equal(page.timers.size, 0);
});
test("pause, hidden visibility and disposal stop polling and ignore late responses", async () => {
  const page = fixture(); page.requests[0].respond(board()); await page.controller.ready;
  await page.tick(); assert.equal(page.requests.length, 2);
  await page.hidden(true);
  assert.equal(page.requests[1].settings.signal.aborted, true); assert.equal(page.timers.size, 0);
  const before = page.node("#board-data").innerHTML;
  const changed = board(); changed.products[0].jobs[0].progress = 90;
  page.requests[1].respond(changed); await flush();
  assert.equal(page.node("#board-data").innerHTML, before);
  await page.hidden(false); assert.equal(page.requests.length, 3);
  page.requests[2].respond(board()); await flush();
  await page.node("#board-pause").emit("click"); assert.equal(page.timers.size, 0);
  await page.hidden(true); await page.hidden(false); assert.equal(page.requests.length, 3);
  const manual = page.node("#board-refresh").emit("click"); await flush();
  assert.equal(page.requests.length, 4);
  page.controller.dispose(); assert.equal(page.requests[3].settings.signal.aborted, true);
  page.requests[3].respond(changed); await manual;
  assert.equal(page.timers.size, 0); assert.equal(page.docListeners.size, 0); assert.equal(page.winListeners.size, 0);
  assert.equal(page.node("#board-data").innerHTML, before);
});
test("revocation clears old board, stops polling and does not echo server task content", async () => {
  const page = fixture(); page.requests[0].respond(board()); await page.controller.ready;
  await page.tick(); page.requests[1].reject(Object.assign(new Error("PRIVATE_SERVER_CONTENT"), { status: 403 })); await flush();
  assert.equal(page.node("#board-data").innerHTML, ""); assert.equal(page.timers.size, 0);
  assert.match(page.node("#board-status").textContent, /权限已失效/);
  assert.doesNotMatch(page.node("#board-status").textContent, /PRIVATE/);
  assert.equal(page.node("#board-refresh").disabled, true);
  assert.equal(page.node("#board-view").disabled, true);
  assert.doesNotMatch(page.node("#board-product").innerHTML, /Document pipeline/);
  assert.match(page.node("#board-updated").textContent, /最近成功刷新/);
});
test("bad DTO and scope drift fail closed without showing unrelated task information", async () => {
  for (const mutate of [
    (data) => { data.shop.id = ids.otherShop; },
    (data) => { data.scope.product_ids.push(ids.otherProduct); },
    (data) => { data.products[0].jobs[0].progress = 101; },
    (data) => { data.products[0].jobs[0].steps[0].state = "secret-custom-label"; },
    (data) => { data.pagination.offset = 100; },
  ]) {
    const page = fixture(), data = board(); mutate(data);
    page.requests[0].respond(data); await page.controller.ready;
    assert.equal(page.node("#board-data").innerHTML, "");
    assert.match(page.node("#board-status").textContent, /当前进度不可用/);
    page.controller.dispose();
  }
});
test("changing view aborts old data and keeps processed separate", async () => {
  const page = fixture(); page.requests[0].respond(board()); await page.controller.ready;
  await page.tick();
  page.node("#board-view").value = "processed"; await page.node("#board-view").emit("change");
  assert.equal(page.requests[1].settings.signal.aborted, true);
  assert.match(page.requests[2].path, /view=processed/);
  const late = board(); late.products[0].name = "OLD_VIEW_PRODUCT";
  page.requests[1].respond(late); await flush(); assert.doesNotMatch(page.node("#board-data").innerHTML, /OLD_VIEW/);
  const done = board(); done.products[0].jobs[0].state = "rejected"; done.products[0].jobs[0].progress = 37;
  page.requests[2].respond(done); await flush();
  assert.match(page.node("#board-data").innerHTML, /已拒绝/);
  assert.match(page.node("#board-data").innerHTML, /37%/); // Never invent 100% for processed work.
});
test("monitor prompt only requests board permission and never includes task data", async () => {
  const page = fixture(); page.requests[0].respond(board()); await page.controller.ready;
  await page.node("#board-copy").emit("click");
  assert.equal(page.copies.length, 1);
  const options = page.copies[0][0];
  assert.equal(options.boardOnly, true); assert.deepEqual(Array.from(options.permissions), ["queue.monitor"]);
  assert.equal(options.productId, ids.product); assert.equal(options.shopId, ids.shop);
  assert.doesNotMatch(JSON.stringify(options), /params|message|output|token|secret|queue\.process|queue\.view/);
});
test("late data after authority change is ignored and next timer cleans itself up", async () => {
  const page = fixture(); page.current(false); page.requests[0].respond(board()); await page.controller.ready;
  assert.equal(page.node("#board-data").innerHTML, ""); assert.equal(page.timers.size, 0);
  page.controller.dispose();
  const loaded = fixture(); loaded.requests[0].respond(board()); await loaded.controller.ready;
  loaded.current(false); await loaded.tick();
  assert.equal(loaded.requests.length, 1); assert.equal(loaded.docListeners.size, 0);
});
test("monitor-only app navigation mounts board without reading product or job content", async () => {
  const page = appFixture(), mounts = [];
  page.context.window.ExtoreProgressBoard = { mount(ctx) { mounts.push(ctx); return { dispose() {} }; } };
  page.navigate("/staff");
  page.set({ role: "staff", permissions: ["queue.monitor"], tab: "products", managedProductId: ids.product, authStatus: { role: "staff", permissions: ["queue.monitor"], shop_id: ids.shop, product_id: ids.product, session_id: "monitor-session" } });
  page.context.shell(); await page.context.renderTab();
  assert.equal(vm.runInContext("tab", page.context), "board");
  assert.match(page.node("#app").innerHTML, /进度看板/); assert.doesNotMatch(page.node("#app").innerHTML, /处理队列|data-tab="products"/);
  assert.equal(mounts.length, 1); assert.equal(page.requests.length, 0);
  assert.equal(mounts[0].isCurrent(), true);
  let disposed = 0;
  page.set({ progressBoardController: { dispose() { disposed++; } } });
  page.context.acceptAuth({ role: "staff", permissions: [], shop_id: ids.shop, product_id: ids.product, session_id: "monitor-session" });
  assert.equal(disposed, 1); assert.equal(page.node("#workspace").innerHTML, "");
  assert.equal(mounts[0].isCurrent(), false);
});
test("queue view can open board; unrelated permission cannot", () => {
  const page = appFixture();
  page.set({ role: "staff", permissions: ["queue.view"] });
  assert.equal(page.context.managementTabs().some(([key]) => key === "board"), true);
  page.set({ permissions: ["cards.manage"] });
  assert.equal(page.context.managementTabs().some(([key]) => key === "board"), false);
  const denied = fixture({ auth: { role: "staff", shop_id: ids.shop, permissions: ["cards.manage"] } });
  assert.equal(denied.requests.length, 0); assert.match(denied.root.innerHTML, /没有查看/);
});
test("progress-only link preset selects only queue.monitor and keeps task-content access off", async () => {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ role: "admin", tab: "staff", products: [{ id: ids.product, name: "Pipeline" }], authStatus: { role: "admin", session_id: "owner-session", shop_id: ids.shop } });
  const checks = ["queue.monitor", "queue.view", "queue.process", "queue.retry", "links.delegate"].map((permission) => Object.assign(page.node(permission), { value: permission, checked: true }));
  page.collections.set('[name="link-permission"]', checks);
  const pending = page.context.renderStaff();
  page.requests[0].respond([]); await pending;
  assert.match(page.node("#workspace").innerHTML, /id="preset-monitor"/);
  await page.node("#preset-monitor").emit("click");
  assert.deepEqual(checks.filter((item) => item.checked).map((item) => item.value), ["queue.monitor"]);
  await checks[0].emit("change");
  assert.equal(checks[1].checked, false); assert.equal(checks[2].checked, false);
});
test("mobile and accessibility floor is explicit in board CSS", () => {
  assert.match(css, /min-height:\s*44px/); assert.match(css, /font-size:\s*16px/);
  assert.match(css, /max-width:\s*390px/); assert.match(css, /minmax\(0, 1fr\)/);
  assert.match(css, /overflow-wrap:\s*anywhere/); assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.doesNotMatch(css, /min-width:\s*(?:[4-9]\d\d|\d{4})px|animation:.*infinite|100vw/);
});
