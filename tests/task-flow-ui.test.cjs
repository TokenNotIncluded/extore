const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function fixture() {
  const nodes = new Map(), timers = new Map(), requests = [], results = [], uploads = [];
  let now = 1000000, active = true, writes = 0, id = 0;
  const decode = (value) => String(value).replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  const create = (id) => ({ id, value: "", textContent: "", isConnected: true, disabled: false, hidden: false, checked: false, files: [], style: {}, dataset: {}, listeners: new Map(),
    addEventListener(event, fn) { this.listeners.set(event, fn); },
    emit(event, values = {}) { return this.listeners.get(event)?.({ preventDefault() {}, target: this, ...values }); },
    focus() { this.focused = true; },
    getAttribute(name) { return this.attributes?.[name]; },
  });
  const app = { isConnected: true, querySelector(selector) { return nodes.get(selector.slice(1)) || null; }, querySelectorAll(selector) {
    if (selector === "input,textarea,select") return [...nodes.values()].filter((node) => ["input", "textarea", "select"].includes(node.kind));
    const match = selector.match(/^\[data-([\w-]+)\]$/);
    return match ? [...nodes.values()].filter((node) => Object.hasOwn(node.dataset, match[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase()))) : [];
  } };
  let markup = "";
  Object.defineProperty(app, "innerHTML", { get() { return markup; }, set(value) {
    writes++; markup = value;
    for (const node of nodes.values()) node.isConnected = false;
    nodes.clear();
    for (const tag of markup.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
      const [, kind, attrs] = tag;
      const explicit = attrs.match(/\bid="([^"]+)"/);
      if (!explicit && !attrs.includes("data-tf-")) continue;
      const node = create(explicit?.[1] || "generated-" + ++id);
      node.kind = kind;
      node.attributes = Object.fromEntries([...attrs.matchAll(/([\w-]+)="([^"]*)"/g)].map((match) => [match[1], decode(match[2])]));
      node.checked = /\schecked(?:\s|$)/.test(attrs);
      node.disabled = /\sdisabled(?:\s|$)/.test(attrs);
      node.value = decode(attrs.match(/\bvalue="([^"]*)"/)?.[1] || "");
      for (const attr of attrs.matchAll(/data-([\w-]+)(?:="([^"]*)")?/g)) node.dataset[attr[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = decode(attr[2] || "");
      if (kind === "textarea") node.value = decode(markup.slice(tag.index + tag[0].length).split("</textarea>")[0]);
      if (kind === "select") {
        const options = [...markup.slice(tag.index + tag[0].length).split("</select>")[0].matchAll(/<option\b([^>]*)>/g)];
        const option = options.find((match) => /\bselected\b/.test(match[1])) || options[0];
        node.value = decode(option?.[1].match(/\bvalue="([^"]*)"/)?.[1] || "");
      }
      nodes.set(node.id, node);
    }
  } });
  const context = vm.createContext({ window: {}, document: { hidden: false }, structuredClone, Date: class extends Date { static now() { return now; } },
    setTimeout(fn) { const timer = ++id; timers.set(timer, fn); return timer; }, clearTimeout(timer) { timers.delete(timer); }, console,
  });
  for (const filename of ["task-flow.js", "task-flow-editor.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../extore/static/", filename), "utf8"), context);
  const ctx = { app, product: { name: "阿拉丁神灯" }, job: { id: "job", state: "waiting" }, token: "receipt-private", cardId: "card-1", lang: "zh-CN", receiptUrl: "https://example.test/receipt#receipt-private", active: () => active,
    api(route, body) { return new Promise((resolve) => requests.push({ route, body, resolve })); },
    async upload(route, fields, file) { uploads.push({ route, fields, file }); return { id: "file-" + uploads.length }; },
    onResult: async (value) => results.push(value), refresh: async () => requests.push({ route: "refresh" }), copy: async () => true, notify() {},
  };
  return { context, ctx, app, node: (id) => nodes.get(id), timers, requests, results, uploads,
    get writes() { return writes; }, setActive(value) { active = value; }, advance(seconds) { now += seconds * 1000; }, tick() { const next = [...timers.entries()][0]; if (next) { timers.delete(next[0]); next[1](); } } };
}
const flow = (phase = "input", overrides = {}) => ({ enabled: true, version: 1, flow_epoch: 2, revision: 7, phase, deadline: 1030, server_time: 1000, actions: [phase === "await_start" ? "start" : phase === "display" ? "continue" : "answer"], current: { id: "question_1", kind: "input", prompt: "准备后开始", question: "你的问题", fields: [{ key: "question", label: { "zh-CN": "问题" }, type: "textarea", required: true }] }, shown: [], ...overrides });
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

 test("the first start does not show the question or start a client-side exchange automatically", async () => {
  const page = fixture();
  page.ctx.flow = flow("await_start", { deadline: null, current: { id: "question_1", kind: "input", prompt: "准备后开始", start_policy: "confirm" } });
  assert.equal(page.context.window.ExtoreTaskFlow.render(page.ctx), true);
  assert.equal(page.node("task-flow-field-question"), undefined);
  assert.equal(page.requests.length, 0);
  page.node("task-flow-submit").emit("click"); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(page.requests[0].body)), { token: "receipt-private", card_id: "card-1", flow_epoch: 2, expected_revision: 7 });
  assert.equal(page.requests[0].route, "/task-flow/start");
});

test("polls preserve typed input and deadline while a submission uses the latest revision", async () => {
  const page = fixture(); page.ctx.flow = flow();
  const module = page.context.window.ExtoreTaskFlow;
  module.render(page.ctx);
  page.node("task-flow-field-question").value = "为什么灯会亮？";
  page.advance(5);
  module.render({ ...page.ctx, flow: flow("input", { revision: 9, server_time: 1005 }) });
  assert.equal(page.writes, 1);
  assert.equal(page.node("task-flow-field-question").value, "为什么灯会亮？");
  assert.equal(page.node("task-flow-clock").textContent, "0:25");
  page.node("task-flow-form").emit("submit"); await flush();
  assert.equal(page.requests[0].body.expected_revision, 9);
  assert.equal(page.requests[0].body.values.question, "为什么灯会亮？");
});

test("a false answer is submitted and previous answers are escaped content", async () => {
  const page = fixture(); page.ctx.flow = flow("input", { current: { id: "judge", kind: "input", fields: [{ key: "yes", type: "boolean", required: true, label: { "zh-CN": "是否" } }] }, shown: [{ key: "answer", type: "text", value: '<img src=x onerror="execute()">' }] });
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  assert.match(page.app.innerHTML, /&lt;img/);
  assert.doesNotMatch(page.app.innerHTML, /<img src=x/);
  page.node("task-flow-field-yes").value = "false";
  page.node("task-flow-form").emit("submit"); await flush();
  assert.equal(page.requests[0].body.values.yes, "false");
});

test("image collections upload each attachment under the current node and epoch", async () => {
  const page = fixture(); page.ctx.flow = flow("input", { current: { id: "images", kind: "input", fields: [{ key: "references", type: "images", max_items: 3, required: true }] } });
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.node("task-flow-field-references").files = [{ name: "one.png" }, { name: "two.png" }];
  page.node("task-flow-form").emit("submit"); await flush();
  assert.equal(page.uploads.length, 2);
  assert.equal(page.uploads[0].fields.flow_epoch, 2);
  assert.equal(page.uploads[1].fields.node_id, "images");
  assert.equal(page.uploads[0].fields.card_id, "card-1");
  assert.deepEqual(JSON.parse(page.requests[0].body.values.references), ["file-1", "file-2"]);
});

test("changing receipts after upload does not submit files into a later task", async () => {
  const page = fixture(); page.ctx.flow = flow("input", { current: { id: "upload", kind: "input", fields: [{ key: "brief", type: "file" }] } });
  page.ctx.upload = async () => { page.setActive(false); return { id: "file-1" }; };
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.node("task-flow-field-brief").files = [{ name: "brief.docx" }];
  page.node("task-flow-form").emit("submit"); await flush();
  assert.equal(page.requests.length, 0);
});

test("an expired deadline refreshes once without inventing an advance or resetting time", async () => {
  const page = fixture(); page.ctx.flow = flow();
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.advance(31); page.tick(); await flush();
  assert.equal(page.requests.filter((request) => request.route === "refresh").length, 1);
  assert.match(page.node("task-flow-clock").textContent, /时间已到/);
  page.tick(); await flush();
  assert.equal(page.requests.length, 1);
});

test("late successful actions cannot paint a different receipt", async () => {
  const page = fixture(); page.ctx.flow = flow();
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.node("task-flow-field-question").value = "Question";
  page.node("task-flow-form").emit("submit"); await flush();
  page.setActive(false); page.requests[0].resolve({ id: "job", state: "queued" }); await flush();
  assert.equal(page.results.length, 0);
});

test("a new flow activation discards old secret input instead of retaining it", () => {
  const page = fixture(); page.ctx.flow = flow("input", { current: { id: "code", kind: "input", fields: [{ key: "otp", type: "text", sensitive: true }] } });
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  const old = page.node("task-flow-field-otp"); old.value = "123456";
  page.context.window.ExtoreTaskFlow.render({ ...page.ctx, flow: flow("queued", { flow_epoch: 3, current: { id: "process", kind: "process" }, actions: [] }) });
  assert.equal(old.value, "");
  assert.equal(page.node("task-flow-field-otp"), undefined);
  assert.equal(old.isConnected, false);
});

test("Aladdin defines independent server timers and explicit previous-answer projections", () => {
  const page = fixture(); const editor = page.context.window.ExtoreTaskFlowEditor;
  const definition = editor.validate(editor.presets.aladdin());
  assert.equal(definition.nodes.find((node) => node.id === "question_1").start_policy, "confirm");
  for (const n of [2, 3]) {
    const question = definition.nodes.find((node) => node.id === "question_" + n);
    assert.equal(question.start_policy, "automatic");
    assert.equal(question.show_from.previous_answer.node, "answer_" + (n - 1));
    assert.equal(question.timeout_next, "timed_out");
  }
  assert.equal(definition.nodes.filter((node) => node.kind === "process").length, 3);
  assert.equal(definition.nodes.find((node) => node.id === "finished").result.content.node, "answer_3");
});

test("the editor exposes ordinary field controls and preserves their edits on step switching", () => {
  const page = fixture(); const module = page.context.window.ExtoreTaskFlowEditor;
  const editor = module.mount(page.app, { value: module.presets.aladdin(), active: () => true });
  assert.equal(page.node("tf-fields-0-label").value, "你的问题");
  page.node("tf-fields-0-label").value = "你想知道什么";
  page.node("tf-fields-0-type").value = "boolean";
  page.node("tf-fields-0-type").emit("change");
  const value = editor.getValue();
  assert.equal(value.nodes[0].fields[0].label["zh-CN"], "你想知道什么");
  assert.equal(value.nodes[0].fields[0].type, "boolean");
});

test("invalid timeout destinations are caught without restricting bounded back jumps", () => {
  const page = fixture(), editor = page.context.window.ExtoreTaskFlowEditor;
  const definition = editor.presets.aladdin();
  definition.nodes[0].timeout_next = "question_1";
  assert.doesNotThrow(() => editor.validate(definition));
  definition.nodes[0].timeout_next = "does_not_exist";
  assert.throws(() => editor.validate(definition), /不存在/);
});

test("display stages render their own configured content without treating it as HTML", () => {
  const page = fixture(); page.ctx.flow = flow("display", { current: { id: "review", kind: "display", content: { "zh-CN": "请检查 <b>上一步</b> 的内容后继续。" } } });
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  assert.match(page.app.innerHTML, /请检查 &lt;b&gt;上一步/);
  assert.equal(page.node("task-flow-submit").isConnected, true);
});

test("retrying a rejected answer reuses uploaded IDs instead of filling the card quota again", async () => {
  const page = fixture(); page.ctx.flow = flow("input", { current: { id: "upload", kind: "input", fields: [{ key: "brief", type: "file" }] } });
  let attempts = 0;
  page.ctx.api = async () => { attempts++; throw new Error("Please retry submission"); };
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.node("task-flow-field-brief").files = [{ name: "brief.docx" }];
  page.node("task-flow-form").emit("submit"); await flush();
  page.node("task-flow-form").emit("submit"); await flush();
  assert.equal(attempts, 2);
  assert.equal(page.uploads.length, 1);
});

test("cancellation requires confirmation and uses the current step revision", async () => {
  const page = fixture(); page.ctx.flow = flow("processing", { actions: [], current: { id: "process", kind: "process" } });
  let consent = false; page.ctx.confirm = () => consent;
  page.context.window.ExtoreTaskFlow.render(page.ctx);
  page.node("task-flow-cancel").emit("click"); await flush();
  assert.equal(page.requests.length, 0);
  consent = true; page.node("task-flow-cancel").emit("click"); await flush();
  assert.equal(page.requests[0].route, "/task-flow/cancel");
  assert.equal(page.requests[0].body.flow_epoch, 2);
  assert.equal(page.requests[0].body.expected_revision, 7);
  assert.equal(page.requests[0].body.values, undefined);
});


test("waiting for upload limits never sends old-step attachments after navigation or revision change", async () => {
  for (const changedRevision of [false, true]) {
    const page = fixture(), module = page.context.window.ExtoreTaskFlow;
    const stage = flow("input", { current: { id: "upload", kind: "input", fields: [{ key: "brief", type: "file" }] } });
    page.ctx.flow = stage;
    let release, invoked = false;
    const waiting = new Promise((resolve) => { release = resolve; });
    page.ctx.upload = async (route, fields, file, options) => {
      invoked = true;
      await waiting;
      if (!options?.isCurrent?.()) throw new Error("The submission changed while waiting for upload limits");
      page.uploads.push({ route, fields, file });
      return { id: "old-step-file" };
    };
    module.render(page.ctx);
    page.node("task-flow-field-brief").files = [{ name: "brief.docx" }];
    page.node("task-flow-form").emit("submit"); await flush();
    assert.equal(invoked, true);
    if (changedRevision) module.render({ ...page.ctx, flow: { ...stage, revision: 8 } });
    else page.setActive(false);
    release(); await flush();
    assert.equal(page.uploads.length, 0);
    assert.equal(page.requests.length, 0);
    assert.equal(page.results.length, 0);
  }
});

test("a delayed answer response cannot replace a newer revision of the same displayed input", async () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlow;
  page.ctx.flow = flow();
  module.render(page.ctx);
  page.node("task-flow-field-question").value = "Question";
  page.node("task-flow-form").emit("submit"); await flush();
  module.render({ ...page.ctx, flow: flow("input", { revision: 8 }) });
  page.requests[0].resolve({ id: "job", task_flow: flow("processing", { revision: 7 }) });
  await flush();
  assert.equal(page.results.length, 0);
  assert.equal(page.node("task-flow-field-question").value, "Question");
});

test("the flow diagram represents branch defaults, failure paths and review-only processing timeouts", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  definition.nodes[0].next = { cases: [{ when: { source: { node: "details", field: "requirements" }, op: "exists" }, to: "process" }], default: "not_completed" };
  const model = module.graphModel(definition);
  assert.deepEqual(JSON.parse(JSON.stringify(model.edges.map((edge) => edge.kind))), ["case", "default", "next", "failure", "review"]);
  const positions = model.nodes.map(({ x, y }) => `${x},${y}`);
  assert.equal(new Set(positions).size, definition.nodes.length);
  module.mount(page.app, { value: definition });
  assert.match(page.app.innerHTML, /<svg/);
  assert.match(page.app.innerHTML, /超时 · 核实/);
  assert.match(page.app.innerHTML, /id="tf-case-0-source"/);
  assert.doesNotMatch(page.app.innerHTML, /条件分支.*高级定义编辑/);
});

test("branch controls save ordered cases, exact values and a default without writing JSON", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const editor = module.mount(page.app, { value: module.presets.confirmation() });
  page.node("tf-label").value = "第一步";
  page.node("tf-route-mode").value = "branch";
  page.node("tf-route-mode").emit("change");
  page.node("tf-case-0-op").value = "in";
  page.node("tf-case-0-op").emit("change");
  page.node("tf-case-0-value").value = "exact\n false \n";
  page.node("tf-case-0-to").value = "process";
  page.node("tf-branch-default").value = "not_completed";
  const value = editor.getValue();
  assert.equal(value.nodes[0].label["zh-CN"], "第一步");
  assert.deepEqual(JSON.parse(JSON.stringify(value.nodes[0].next.cases[0].when)), { source: { node: "details", field: "requirements" }, op: "in", value: ["exact", " false ", ""] });
  assert.equal(value.nodes[0].next.default, "not_completed");
});

test("mapping controls preserve source identity and typed unsaved fields when another step is selected", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const editor = module.mount(page.app, { value: module.presets.confirmation() });
  page.node("tf-node-process").emit("click");
  page.node("tf-label").value = "Run edited task";
  page.node("tf-map-inputs-0-key").value = "renamed_input";
  page.node("tf-map-inputs-0-source").value = "details.requirements";
  page.node("tf-node-finished").emit("click");
  assert.equal(page.node("tf-map-result-0-source").value, "process.content");
  const value = editor.getValue();
  assert.equal(value.nodes[1].label["zh-CN"], "Run edited task");
  assert.deepEqual(JSON.parse(JSON.stringify(value.nodes[1].inputs)), { renamed_input: { node: "details", field: "requirements" } });
});

test("node and field deletion cannot break branches, aliases or mappings", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  definition.nodes[0].next = { cases: [{ when: { source: { node: "details", field: "requirements" }, op: "exists" }, to: "process" }], default: "not_completed" };
  const editor = module.mount(page.app, { value: definition });
  page.app.querySelectorAll("[data-tf-remove-field]")[0].emit("click");
  assert.match(page.node("task-flow-editor-error").textContent, /映射或条件/);
  page.node("tf-node-process").emit("click");
  page.node("tf-remove").emit("click");
  assert.match(page.node("task-flow-editor-error").textContent, /路径与字段引用/);
  assert.equal(editor.getValue().nodes.length, 4);
});

test("sensitive sources are available only to process inputs, and the UI does not generate alias chains", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  definition.nodes[0].fields.push({ key: "otp", label: { "zh-CN": "验证码" }, type: "text", required: true, sensitive: true, sensitive_ttl_seconds: 120 });
  definition.nodes.push({ id: "review", kind: "display", show_from: { copied: { node: "process", field: "content" } }, next: "finished" });
  assert.equal(module.sources(definition, "inputs").some((source) => source.reference.field === "otp"), true);
  for (const purpose of ["show_from", "result", "branch"]) assert.equal(module.sources(definition, purpose).some((source) => source.reference.field === "otp"), false);
  assert.equal(module.sources(definition, "inputs").some((source) => source.reference.node === "review"), false);
  definition.nodes[2].result.content = { node: "details", field: "otp" };
  assert.throws(() => module.validate(definition), /敏感字段/);
});

test("readonly step navigation does not rewrite values and stale editor controls cannot mutate a replacement page", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  const editor = module.mount(page.app, { value: definition, disabled: true });
  page.node("tf-label").value = "cannot mutate";
  page.node("tf-node-process").emit("click");
  assert.equal(editor.getValue().nodes[0].label["zh-CN"], "提交需求");
  const oldNode = page.node("tf-node-finished"), oldAdd = page.node("tf-add");
  editor.dispose();
  page.app.innerHTML = "Replacement page";
  oldNode.emit("click"); oldAdd.emit("click");
  assert.equal(page.app.innerHTML, "Replacement page");
  assert.throws(() => editor.getValue(), /已失效/);
});

test("server configuration validation uses current product fields and cannot overwrite edits made while waiting", async () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  let resolve, received;
  const editor = module.mount(page.app, { value: module.presets.confirmation(), product: () => ({ mode: "manual", parameters: [], outputs: [{ key: "content", type: "textarea" }] }), validate: (definition, product) => { received = { definition, product }; return new Promise((done) => { resolve = done; }); } });
  page.node("tf-label").value = "Submitted label";
  const pending = page.node("tf-check").emit("click");
  assert.equal(received.definition.nodes[0].label["zh-CN"], "Submitted label");
  assert.equal(received.product.outputs[0].key, "content");
  assert.equal(page.node("tf-check").disabled, true);
  page.node("tf-label").value = "A newer edit";
  page.node("tf-label").emit("input");
  resolve({ ok: true, definition: module.presets.aladdin() }); await pending;
  assert.equal(page.node("tf-validation-status").textContent, "");
  assert.equal(editor.getValue().nodes[0].label["zh-CN"], "A newer edit");
  assert.equal(editor.getValue().nodes.length, 4);
});

test("validation failures keep the form, report product-capture errors and ignore responses after step switching", async () => {
  for (const captureError of [true, false]) {
    const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
    let reject;
    module.mount(page.app, { value: module.presets.confirmation(), product: () => { if (captureError) throw new Error("Bad current product schema"); return {}; }, validate: () => new Promise((_, fail) => { reject = fail; }) });
    page.node("tf-label").value = "Preserve me";
    const pending = page.node("tf-check").emit("click");
    if (!captureError) { page.node("tf-node-process").emit("click"); reject(new Error("Old response")); }
    await pending;
    if (captureError) { assert.equal(page.node("tf-label").value, "Preserve me"); assert.match(page.node("task-flow-editor-error").textContent, /Bad current product/); }
    else assert.equal(page.node("task-flow-editor-error").textContent, "");
  }
});

test("JSON export preserves the draft and English configuration labels remain accessible", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  let exported;
  module.mount(page.app, { value: module.presets.confirmation(), lang: "en", export: (value) => { exported = value; } });
  page.node("tf-label").value = "Exact English name";
  page.node("tf-export-json").emit("click");
  assert.equal(exported.nodes[0].label.en, "Exact English name");
  assert.match(page.app.innerHTML, /Customer fields/);
  assert.match(page.app.innerHTML, /aria-label="Flow diagram; select a step to edit"/);
  assert.equal(page.node("tf-label").value, "Exact English name");
});

test("graph validation follows server ID, entry, source and UTF-8 size restrictions", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  for (const mutate of [
    (value) => { value.nodes[0].id = "Details"; value.entry = "Details"; },
    (value) => { value.entry = "process"; },
    (value) => { value.nodes[1].inputs.requirements.field = "unknown"; },
    (value) => { value.nodes[0].prompt["zh-CN"] = "中".repeat(35000); },
  ]) { const value = module.presets.confirmation(); mutate(value); assert.throws(() => module.validate(value)); }
  const value = module.presets.confirmation(); value.nodes[0].timeout_seconds = 5; value.nodes[0].timeout_next = "details";
  assert.doesNotThrow(() => module.validate(value));
});

test("changing the product schema while validation waits invalidates both success and failure feedback", async () => {
  for (const failed of [false, true]) {
    const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
    let finish, fail, outputKey = "content";
    module.mount(page.app, { value: module.presets.confirmation(), product: () => ({ mode: "manual", parameters: [], outputs: [{ key: outputKey, type: "textarea" }] }), validate: () => new Promise((resolve, reject) => { finish = resolve; fail = reject; }) });
    const pending = page.node("tf-check").emit("click");
    outputKey = "new_output";
    if (failed) fail(new Error("Obsolete schema error")); else finish({ ok: true });
    await pending;
    assert.equal(page.node("tf-validation-status").textContent, "");
    assert.equal(page.node("task-flow-editor-error").textContent, "");
    assert.equal(page.node("tf-check").disabled, false);
  }
});

test("a current product-capture error on validation completion cannot leave old success feedback", async () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  let finish, invalidProduct = false;
  module.mount(page.app, { value: module.presets.confirmation(), product: () => { if (invalidProduct) throw new Error("Current output field is invalid"); return { mode: "manual", outputs: [] }; }, validate: () => new Promise((resolve) => { finish = resolve; }) });
  const pending = page.node("tf-check").emit("click");
  invalidProduct = true; finish({ ok: true }); await pending;
  assert.equal(page.node("tf-validation-status").textContent, "");
  assert.match(page.node("task-flow-editor-error").textContent, /Current output field/);
});

test("untouched legal choice values and multiline translated labels survive mount and capture byte for byte", () => {
  for (const locale of ["zh-CN", "en"]) {
    const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
    const definition = module.presets.confirmation();
    const options = [
      { value: "word|ppt", label: { "zh-CN": "文档\n演示", en: "Document\nPresentation" } },
      { value: "normal", label: { "zh-CN": "  正常选项  ", en: "  Normal choice  " } },
    ];
    definition.nodes[0].fields = [{ key: "requirements", type: "select", label: { "zh-CN": "规格" }, options }];
    const editor = module.mount(page.app, { value: definition, lang: locale });
    assert.match(page.app.innerHTML, /id="tf-fields-0-choices"[^>]*readonly/);
    assert.deepEqual(JSON.parse(JSON.stringify(editor.getValue().nodes[0].fields[0].options)), options);
    // Even a programmatic edit cannot reinterpret the ambiguous display form.
    page.node("tf-fields-0-choices").value = "word | wrong label";
    assert.deepEqual(JSON.parse(JSON.stringify(editor.getValue().nodes[0].fields[0].options)), options);
  }
});

test("simple choice projections only parse on an actual edit and omitted required means checked", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  const options = [{ value: "word", label: { "zh-CN": " Word ", en: "Word" } }, { value: "ppt", label: { "zh-CN": "PPT", en: "Slides" } }];
  definition.nodes[0].fields = [{ key: "requirements", type: "select", label: { "zh-CN": "规格" }, options }];
  const editor = module.mount(page.app, { value: definition });
  assert.equal(page.node("tf-fields-0-required").checked, true);
  assert.equal(editor.getValue().nodes[0].fields[0].required, true);
  assert.deepEqual(JSON.parse(JSON.stringify(editor.getValue().nodes[0].fields[0].options)), options);
  page.node("tf-fields-0-choices").value = "word | 文档\nppt | 演示";
  const changed = editor.getValue().nodes[0].fields[0].options;
  assert.equal(changed[0].value, "word");
  assert.equal(changed[0].label["zh-CN"], "文档");
  assert.equal(changed[0].label.en, "Word");
  assert.equal(changed[1].label.en, "Slides");
});

test("fit and full-size controls affect only the bounded diagram and preserve unsaved configuration", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const editor = module.mount(page.app, { value: module.presets.aladdin() });
  page.node("tf-label").value = "Keep this draft";
  page.node("tf-fit").emit("click");
  assert.equal(page.node("tf-fit").getAttribute("aria-pressed"), "true");
  assert.match(page.app.innerHTML, /transform:scale\(0\./);
  assert.equal(editor.getValue().nodes[0].label["zh-CN"], "Keep this draft");
  page.node("tf-actual-size").emit("click");
  assert.equal(page.node("tf-actual-size").getAttribute("aria-pressed"), "true");
  assert.match(page.app.innerHTML, /transform:scale\(1\)/);
  assert.equal(editor.getValue().nodes[0].label["zh-CN"], "Keep this draft");
});


test("raw v1 process deadlines use the server default when omitted", () => {
  const page = fixture(), module = page.context.window.ExtoreTaskFlowEditor;
  const definition = module.presets.confirmation();
  delete definition.nodes[1].timeout_seconds;
  assert.doesNotThrow(() => module.validate(definition));
  const editor = module.mount(page.app, { value: definition });
  page.node("tf-node-process").emit("click");
  assert.equal(page.node("tf-timeout").value, "3600");
  assert.equal(editor.getValue().nodes[1].timeout_seconds, 3600);
  for (const invalid of [null, 0, false, -1, 86401]) {
    definition.nodes[1].timeout_seconds = invalid;
    assert.throws(() => module.validate(definition));
  }
});
