const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const { appFixture, flush, product, parameter, job } = require("./app-fixture.cjs");

function page(language = "zh-CN") {
  const p = appFixture();
  const content = p.node("#content"), host = p.node("#app");
  const children = new Set(), blobs = new Map(), created = [], revoked = [], copied = [];
  let markup = "", hostMarkup = "", urlCount = 0;
  function detach() { for (const child of children) child.isConnected = false; children.clear(); }
  Object.defineProperty(content, "innerHTML", {
    get: () => markup,
    set(value) {
      detach(); markup = String(value);
      for (const match of markup.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/gi)) {
        const child = p.node("#" + match[2]);
        child.isConnected = true;
        child.hidden = /\bhidden\b/.test(match[0]);
        child.tag = match[1];
        children.add(child);
      }
    },
  });
  Object.defineProperty(host, "innerHTML", {
    get: () => hostMarkup,
    set(value) { detach(); markup = ""; hostMarkup = String(value); },
  });
  content.contains = (child) => children.has(child);
  p.context.navigator.clipboard = { async writeText(text) { copied.push(text); } };
  class DownloadURL extends URL {
    static createObjectURL(blob) {
      const url = "blob:receipt-text-" + ++urlCount;
      blobs.set(url, blob); created.push({ url, blob }); return url;
    }
    static revokeObjectURL(url) { revoked.push(url); blobs.delete(url); }
  }
  p.context.URL = DownloadURL;
  p.navigate("/receipt#receipt-token");
  const definition = product("Customer name must not become a filename");
  p.set({ currentToken: "receipt-token", currentProduct: definition, currentJob: job("job-one"), lang: language });
  return { ...p, definition, copied, blobs, created, revoked,
    buttons() { return [...children].filter((child) => child.tag === "button"); },
  };
}

async function reveal(p, output, extra = {}, options = {}) {
  const pending = p.context.revealReceipt(options);
  const request = p.requests[p.requests.length - 1];
  request.respond({ output, ...extra });
  await pending;
  return request;
}

function control(p, action, index = 0) {
  const ids = [...p.node("#content").innerHTML.matchAll(new RegExp(`id="(delivery-text-[0-9]+-[0-9]+-${action})"`, "g"))];
  assert.ok(ids[index], "Expected an existing revealed text control");
  return p.node("#" + ids[index][1]);
}
function status(p, button) {
  const id = [...p.node("#content").innerHTML.matchAll(/id="([^"]+-copy)"/g)].find((match) => p.node("#" + match[1]) === button)?.[1];
  return p.node("#" + id.replace(/-copy$/, "-status"));
}

test("once delivery adds controls only after reveal and copies/downloads the exact original without further requests", async () => {
  const p = page();
  p.definition.view_policy = "once";
  p.context.renderReceipt({ ...job("job-one"), view_policy: "once" });
  assert.doesNotMatch(p.node("#app").innerHTML, /delivery-text-\d+-\d+-copy/);
  const text = "  第一行\r\n<script>untrusted()</script>\n最后一行\t  ";
  const pending = p.context.revealReceipt();
  assert.equal(p.buttons().length, 0);
  p.requests[0].respond({ content: text });
  await pending;
  assert.equal(p.node("#reveal").removed, true);
  assert.match(p.node("#content").innerHTML, /&lt;script&gt;untrusted\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(p.node("#content").innerHTML, /<script>/);
  await control(p, "copy").emit("click");
  await control(p, "download").emit("click");
  assert.deepEqual(p.copied, [text]);
  assert.equal(await p.created[0].blob.text(), text);
  assert.equal(p.created[0].blob.type, "text/plain;charset=utf-8");
  assert.equal(p.downloads[0].filename, "extore-content.txt");
  assert.equal(p.requests.length, 1);
  assert.match(status(p, control(p, "copy")).textContent, /下载/);
});

test("boolean/select labels remain readable while both actions retain protocol values", async () => {
  for (const [language, yes, option] of [["zh-CN", "是", "强化版"], ["en", "Yes", "Enhanced"]]) {
    const p = page(language);
    p.definition.outputs = [parameter("approved", "boolean"), {
      ...parameter("tier", "select"), options: [{ value: "enhanced", label: { "zh-CN": "强化版", en: "Enhanced" } }],
    }];
    await reveal(p, { approved: "true", tier: "enhanced" });
    assert.match(p.node("#content").innerHTML, new RegExp(`<pre class="result">${yes}</pre>`));
    assert.match(p.node("#content").innerHTML, new RegExp(`<pre class="result">${option}</pre>`));
    await control(p, "copy", 0).emit("click");
    await control(p, "copy", 1).emit("click");
    await control(p, "download", 1).emit("click");
    assert.deepEqual(p.copied, ["true", "enhanced"]);
    assert.equal(await p.created[0].blob.text(), "enhanced");
    assert.match(p.node("#content").innerHTML, /role="status" aria-live="polite"/);
    assert.match(p.node("#content").innerHTML, language === "en" ? /Copy original text:/ : /复制原文：/);
  }
});

test("JSON download preserves formatting and numeric spelling; ordinary/invalid text stays TXT", async () => {
  const values = {
    formatted_json: "\n{\"value\":12345678901234567890,\"tiny\":1E-003}\n",
    summary: "null",
    details: "[\"first\",\"second\"]",
    broken_json: "{not valid JSON}",
    prose: "# Customer instructions\n\nDo not execute this content.",
  };
  const p = page();
  await reveal(p, values);
  for (let index = 0; index < Object.keys(values).length; index++) await control(p, "download", index).emit("click");
  assert.deepEqual(p.downloads.map((entry) => entry.filename), ["extore-formatted_json.json", "extore-summary.json", "extore-details.json", "extore-broken_json.txt", "extore-prose.txt"]);
  for (let index = 0; index < p.created.length; index++) assert.equal(await p.created[index].blob.text(), Object.values(values)[index]);
});

test("untrusted field keys/labels remain escaped, safe filenames and own prototype-like output keys work", async () => {
  const p = page();
  p.definition.outputs = [{ ...parameter("__proto__"), label: { "zh-CN": '<img src=x onerror="attack()">' } }];
  const values = JSON.parse('{"__proto__":"prototype value","constructor":"constructor value","../../<script>/\\\"":"hostile key value"}');
  await reveal(p, values);
  assert.match(p.node("#content").innerHTML, /&lt;img src=x onerror=&quot;attack\(\)&quot;&gt;/);
  assert.doesNotMatch(p.node("#content").innerHTML, /<(?:img|script)\b/);
  for (let index = 0; index < 3; index++) { await control(p, "copy", index).emit("click"); await control(p, "download", index).emit("click"); }
  assert.deepEqual(p.copied, Object.values(values));
  assert.ok(p.downloads.every((entry) => /^extore-[a-z0-9_-]+\.txt$/.test(entry.filename)));
  assert.equal({}.polluted, undefined);
  assert.ok(p.downloads.every((entry) => !entry.filename.includes("Customer name")));
});

test("attachment fields retain existing download actions and never get text copy/download controls", async () => {
  const p = page();
  const id = "ab000000-0000-4000-8000-000000000001";
  p.definition.outputs = [parameter("document", "file"), parameter("images", "images"), parameter("remarks", "textarea")];
  await reveal(p, { document: id, images: "[]", remarks: "Delivery remarks" }, { files: [{ id, field_key: "document", filename: "report.docx", size: 120, content_type: "application/octet-stream" }] });
  const markup = p.node("#content").innerHTML;
  assert.match(markup, new RegExp(`data-delivery-file="${id}"`));
  assert.equal(p.buttons().filter((button) => button === control(p, "copy")).length, 1);
  assert.equal([...markup.matchAll(/id="delivery-text-\d+-\d+-copy"/g)].length, 1);
  await control(p, "copy").emit("click");
  assert.deepEqual(p.copied, ["Delivery remarks"]);
});

test("clipboard failure exposes and selects the original safely with localized guidance", async () => {
  for (const language of ["zh-CN", "en"]) {
    const p = page(language);
    p.context.navigator.clipboard.writeText = async () => { throw new Error("Permission denied"); };
    const text = "  <svg onload=attack()>\nraw\t";
    await reveal(p, { content: text });
    const copy = control(p, "copy");
    await copy.emit("click");
    const id = [...p.node("#content").innerHTML.matchAll(/id="([^"]+-copy)"/g)][0][1].replace(/-copy$/, "");
    const manual = p.node("#" + id + "-manual"), raw = p.node("#" + id + "-raw");
    assert.equal(manual.hidden, false);
    assert.equal(raw.value, text);
    assert.equal(raw.focused, true);
    assert.equal(raw.selected, true);
    assert.equal(copy.disabled, false);
    assert.match(status(p, copy).textContent, language === "en" ? /original text below/ : /下方原文/);
    assert.doesNotMatch(p.node("#content").innerHTML, /<svg/);
  }
});

test("old controls cannot operate after route, token, batch-card, generation, or DOM changes", async () => {
  const changes = [
    (p) => p.navigate("/admin"),
    (p) => p.navigate("/receipt#other-token"),
    (p) => p.set({ currentToken: "other-token" }),
    (p) => p.context.selectBatchCard("another-card"),
    (p) => vm.runInContext("receiptGeneration++", p.context),
    (p) => p.set({ currentJob: job("another-job") }),
    (p) => p.set({ currentJob: { ...job("job-one"), revision: { current: 1 } } }),
    (p) => p.set({ currentJob: job("job-one", "destroyed") }),
    (p) => { p.node("#content").innerHTML = "Replacement delivery"; },
    (p) => { p.node("#app").innerHTML = "New page"; },
  ];
  for (const change of changes) {
    const p = page();
    await reveal(p, { content: "Private old result" });
    const copy = control(p, "copy"), download = control(p, "download");
    change(p);
    await copy.emit("click"); await download.emit("click");
    assert.deepEqual(p.copied, []);
    assert.equal(p.created.length, 0);
    assert.equal(p.downloads.length, 0);
    assert.equal(p.requests.length, 1);
  }
});

test("new reveal/revision invalidates old controls even while its request is still pending", async () => {
  const p = page();
  await reveal(p, { content: "Old version" }, {}, { revision: 0 });
  const oldCopy = control(p, "copy"), oldDownload = control(p, "download");
  const second = p.context.revealReceipt({ revision: 1 });
  await oldCopy.emit("click"); await oldDownload.emit("click");
  assert.equal(p.copied.length, 0);
  assert.equal(p.downloads.length, 0);
  p.requests[1].respond({ revision: 1, content: "New version" });
  await second;
  await oldCopy.emit("click"); await oldDownload.emit("click");
  await control(p, "copy").emit("click");
  assert.deepEqual(p.copied, ["New version"]);
  assert.equal(p.requests.length, 2);
});

test("version selection invalidates text controls and revokes local download URLs", async () => {
  const p = page();
  p.context.renderReceipt({ ...job("job-one"), deliveries: [{ revision: 0 }, { revision: 1 }], last_delivery: { revision: 1 } });
  await reveal(p, { content: "Version zero" }, {}, { revision: 0 });
  const copy = control(p, "copy"), download = control(p, "download");
  await download.emit("click");
  assert.equal(p.blobs.size, 1);
  await p.node("#delivery-revision").emit("change");
  assert.equal(p.blobs.size, 0);
  assert.equal(p.revoked.length, 1);
  await copy.emit("click"); await download.emit("click");
  assert.equal(p.copied.length, 0);
  assert.equal(p.downloads.length, 1);
  for (const timer of p.timers.values()) timer();
  assert.equal(p.revoked.length, 1, "Cleanup timers do not revoke a released URL twice");
});

test("successful destruction invalidates old text actions before the receipt refresh finishes", async () => {
  const p = page();
  await reveal(p, { content: "Destroyed result" });
  const copy = control(p, "copy"), download = control(p, "download");
  await download.emit("click");
  const destroying = p.context.destroyReceipt();
  p.requests[1].respond({ state: "destroyed" });
  await flush();
  await copy.emit("click"); await download.emit("click");
  assert.deepEqual(p.copied, []);
  assert.equal(p.downloads.length, 1);
  assert.equal(p.blobs.size, 0);
  p.requests[2].respond({ product: p.definition, job: job("job-one", "destroyed") });
  await destroying;
  assert.doesNotMatch(p.node("#app").innerHTML, /delivery-text-\d+-\d+-copy/);
});

test("a late clipboard completion leaves a new receipt untouched", async () => {
  const p = page();
  let complete;
  p.context.navigator.clipboard.writeText = () => new Promise((resolve) => { complete = resolve; });
  await reveal(p, { content: "Already revealed" });
  const copy = control(p, "copy"), oldStatus = status(p, copy);
  const clicking = copy.emit("click");
  p.navigate("/receipt#new-token");
  p.node("#app").innerHTML = "Another receipt";
  complete();
  await clicking;
  assert.equal(oldStatus.textContent, "");
  assert.equal(p.node("#app").innerHTML, "Another receipt");
});

test("local download failure gives accessible fallback feedback without uploading or revealing again", async () => {
  const p = page("en");
  await reveal(p, { content: "Save me" });
  p.context.URL.createObjectURL = () => { throw new Error("No object URL support"); };
  await control(p, "download").emit("click");
  assert.match(status(p, control(p, "copy")).textContent, /Copy the text to save it/);
  assert.equal(p.requests.length, 1);
  assert.equal(p.downloads.length, 0);
});
