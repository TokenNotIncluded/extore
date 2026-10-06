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
    emit(event) { return this.listeners.get(event)?.({ preventDefault() {}, target: this }); },
    getAttribute(name) { return this.attributes?.[name]; },
  });
  const app = { isConnected: true, querySelector(selector) { return nodes.get(selector.slice(1)) || null; }, querySelectorAll(selector) {
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
