const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { appFixture, flush, product, job } = require("./app-fixture.cjs");
const identitySource = fs.readFileSync(path.join(__dirname, "../extore/static/worker-identity.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../extore/static/queue.css"), "utf8");
async function queue(rows = [], values = {}) {
  const page = appFixture(); page.navigate("/admin");
  page.set({ role: "admin", tab: "jobs", ...values });
  vm.runInContext(identitySource, page.context);
  const checks = rows.map((row) => Object.assign(page.node("#check-" + row.id), { value: row.id, checked: false }));
  page.collections.set("[name=job]", checks);
  const pending = page.context.renderJobs("", "product");
  page.requests[0].respond([product("Long document pipeline")]); await flush();
  page.requests[1].respond(rows); await pending;
  return { ...page, checks };
}
const task = (state, extra = {}) => ({ ...job("task-one", state), product_id: "product", params: { requirement: "Material" }, files: [], claimed_by: state === "processing" ? "owner" : null, ...extra });
test("empty queue keeps scope, refresh and AI entry without irrelevant bulk controls", async () => {
  const page = await queue(); const html = page.node("#workspace").innerHTML;
  assert.equal((html.match(/<h2>/g) || []).length, 1);
  assert.match(html, /<h2>处理队列<\/h2>/);
  assert.match(html, /id="queue-product"|id="refresh"|id="copy-queue-ai"/);
  assert.match(html, /队列暂时空着/);
  assert.doesNotMatch(html, /id="claim"|id="complete"|id="fail"|id="reject"|id="release"|<table/);
});
test("task selection reveals bulk actions and enables only applicable own work", async () => {
  const page = await queue([task("queued")]);
  assert.equal(page.node("#queue-bulk").hidden, true);
  assert.equal(page.node("#claim").disabled, true);
  page.checks[0].checked = true; await page.checks[0].emit("change");
  assert.equal(page.node("#queue-bulk").hidden, false);
  assert.equal(page.node("#claim").disabled, false);
  assert.equal(page.node("#progress-update").disabled, true);
  assert.equal(page.node("#reject").disabled, true);
  assert.match(page.node("#workspace").innerHTML, /<details class="queue-more">/);
  const own = await queue([task("processing")]);
  own.checks[0].checked = true; await own.checks[0].emit("change");
  assert.equal(own.node("#claim").disabled, true);
  assert.equal(own.node("#progress-update").disabled, false);
  assert.equal(own.node("#complete").disabled, false);
  const other = await queue([task("processing", { claimed_by: "another-worker" })]);
  other.checks[0].checked = true; await other.checks[0].emit("change");
  assert.equal(other.node("#complete").disabled, true);
});
test("collapsed task details retain safe snapshot parameters and file controls", async () => {
  const page = await queue([task("processing", { params: { requirement: "safe material", otp: "PRIVATE_OTP" }, protected_fields: ["otp"], files: [{ id: "file-one", kind: "input", filename: "report.pdf", size: 1024 }] })]);
  const html = page.node("#workspace").innerHTML;
  assert.match(html, /<details class="queue-input"><summary>/);
  assert.match(html, /safe material|data-management-download="file-one"/);
  assert.doesNotMatch(html, /PRIVATE_OTP|<details class="queue-input" open/);
});
test("worker cartoons use only fixed local assets and self-reported type is unverified", async () => {
  const page = await queue([task("processing", { processing_worker: { name: '<img src="https://evil.test/avatar">', kind: "cli", agent_type: "dots" } })]);
  const html = page.node("#workspace").innerHTML;
  assert.match(html, /src="\/static\/worker-avatars\/dots\.webp"/);
  assert.match(html, /alt=""/); assert.match(html, /自报类型：dots · 未验证/);
  assert.doesNotMatch(html, /<img src="https:/);
  const identity = page.context.window.ExtoreWorkerIdentity;
  for (const type of ["grok_bot", "grok bot", "Grok-Bot"])
    assert.match(identity.markup({ name: "Grok worker", kind: "cli", agent_type: type }), /\/grok-bot\.webp/);
  for (const type of ["constructor", "__proto__", "some-custom-bot", "https://evil.test/custom.png"])
    assert.match(identity.markup({ name: "Bot", kind: "cli", agent_type: type }), /\/other\.webp/);
  assert.match(identity.markup({ name: "Handler", kind: "automatic", agent_type: "processor" }), /\/processor\.webp/);
  assert.match(identity.markup({ name: "Staff", kind: "human", agent_type: null }), /\/human\.webp/);
  assert.equal(identity.markup({ name: "Bot", kind: "cli", agent_type: "dots\u202eevil" }), "");
});
test("successful AI copy leaves preview collapsed; clipboard failure expands selected fallback", async () => {
  for (const success of [true, false]) {
    const page = appFixture();
    page.context.window.ExtoreCliPrompts = { build: () => "SAFE_AI_PROMPT" };
    page.context.navigator.clipboard = { writeText: async () => { if (!success) throw new Error("Denied"); } };
    await page.context.copyCLIPrompt({ origin: "https://example.test" }, page.node("#queue-ai-prompt"));
    assert.match(page.node("#queue-ai-prompt").innerHTML, /<details id="cli-ai-preview"/);
    assert.equal(page.node("#cli-ai-preview").open === true, !success);
    assert.equal(page.node("#cli-ai-prompt").selected === true, !success);
    assert.match(page.node("#cli-ai-feedback").textContent, success ? /已复制/ : /手动复制/);
  }
});
test("queue typography and controls meet narrow/mobile accessibility limits", () => {
  assert.match(css, /min-height:\s*44px/); assert.match(css, /font-size:\s*16px/);
  assert.match(css, /minmax\(0, 1fr\)/); assert.match(css, /max-width:\s*390px/);
  assert.match(css, /white-space:\s*pre-wrap/); assert.match(css, /overflow-wrap:\s*anywhere/);
  assert.match(css, /prefers-reduced-motion/); assert.doesNotMatch(css, /100vw|animation:.*infinite/);
});
