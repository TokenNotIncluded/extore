const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/text-cards.js"), "utf8");
const context = vm.createContext({ window: {}, TextEncoder });
vm.runInContext(source, context);
const parse = (value) => JSON.parse(JSON.stringify(context.window.ExtoreTextCards.parse(value)));

test("text import preview preserves distinct lines, removes BOM and current duplicates", () => {
  assert.deepEqual(parse("\uFEFF  first \r\n\r\nA  B\rfirst\r\n"), {
    items: ["first", "A  B"], lines: 4, blank: 1, duplicates: 1, created: 2,
  });
  assert.deepEqual(parse("x\nx\nX\nx y\nx  y").items, ["x", "X", "x y", "x  y"]);
});

test("text import bounds UTF8 bytes, Unicode characters and unique item count", () => {
  assert.throws(() => parse("汉".repeat(700000)), /2 MiB/);
  assert.throws(() => parse("🪔".repeat(10001)), /10,000/);
  assert.equal(parse("🪔".repeat(10000)).created, 1);
  assert.throws(() => parse(Array.from({ length: 1001 }, (_, index) => String(index)).join("\n")), /1,000/);
  assert.equal(parse("same\n".repeat(2000)).created, 1);
  assert.throws(() => parse("unsafe\0text"), /控制字符/);
});

test("import code keeps content out of markup, URLs and browser storage", () => {
  assert.match(source, /text-cards-generated"\)\.value = codes\.join/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|location\.hash|\?text=/);
  assert.match(source, /new TextDecoder\("utf-8", \{ fatal: true \}\)/);
});

function page(handler) {
  const nodes = new Map(), requests = [], copied = [], busy = [];
  class Element {
    constructor(id, tag = "div") {
      Object.assign(this, { id, tag, value: "", textContent: "", disabled: false, isConnected: true });
      this.listeners = new Map(); this.children = [];
    }
    set innerHTML(value) {
      this.html = value;
      for (const id of this.children) nodes.delete(id);
      this.children = [];
      for (const match of value.matchAll(/<([a-z]+)\b([^>]*)>/g)) {
        const [, tag, attrs] = match, id = attrs.match(/\bid="([^"]+)"/)?.[1];
        if (!id) continue;
        const child = new Element(id, tag); nodes.set(id, child); this.children.push(id);
        child.disabled = /\bdisabled\b/.test(attrs);
        if (tag === "select") child.value = value.slice(match.index).match(/<option value="([^"]+)"/)?.[1] || "";
      }
    }
    get innerHTML() { return this.html || ""; }
    querySelector(selector) { return nodes.get(selector.slice(1)); }
    querySelectorAll() { return [...nodes.values()]; }
    addEventListener(event, callback) { this.listeners.set(event, callback); }
    emit(event, target = this) { return this.listeners.get(event)?.({ target, preventDefault() {} }); }
    focus() { this.focused = true; } select() { this.selected = true; }
  }
  const root = new Element("root"), window = { ExtoreClipboard: { async writeText(value) { copied.push(value); return true; } } };
  const context = vm.createContext({ window, TextEncoder, TextDecoder, AbortController, Blob, URL, JSON, navigator: {}, setTimeout });
  vm.runInContext(source, context);
  const control = window.ExtoreTextCards.mount({
    root, product: { id: "text-product" }, variants: [{ id: "word", name: "Word" }],
    endpoint: "/manage", onBusy: (value) => busy.push(value),
    async api(url, body) { requests.push({ url, body }); return handler(url, body); },
  });
  return { root, control, node: (id) => nodes.get(id), requests, copied, busy };
}

test("paste import submits scoped stock and copy uses only the current response", async () => {
  const p = page(async () => ({ codes: ["NEW-CODE"], items: [{ code: "NEW-CODE", content: "safe text" }], stats: { blank: 1, duplicates: 1 }, batch_id: "batch-1" }));
  p.node("text-cards-text").value = "safe text\nsafe text\n";
  await p.node("text-cards-form").emit("submit");
  assert.equal(p.requests[0].url, "/manage/cards/import-text");
  assert.equal(p.requests[0].body.product_id, "text-product");
  assert.equal(p.requests[0].body.variant_id, "word");
  assert.equal(p.node("text-cards-text").value, "");
  assert.equal(p.node("text-cards-generated").value, "NEW-CODE");
  await p.node("text-cards-copy").emit("click");
  assert.deepEqual(p.copied, ["NEW-CODE"]);
  assert.deepEqual(p.busy, [true, false]);
});

test("UTF8 file import populates the form while invalid files preserve the draft", async () => {
  const p = page(async () => ({}));
  p.node("text-cards-text").value = "old draft";
  const file = p.node("text-cards-file");
  file.files = [{ size: 20, async arrayBuffer() { return new TextEncoder().encode("\uFEFFfirst\r\nsecond\n").buffer; } }];
  await file.emit("change");
  assert.equal(p.node("text-cards-text").value, "first\r\nsecond\n");
  assert.match(p.node("text-cards-preview").textContent, /2 张/);
  file.files = [{ size: 1, async arrayBuffer() { return new Uint8Array([255]).buffer; } }];
  await file.emit("change");
  assert.equal(p.node("text-cards-text").value, "first\r\nsecond\n");
  assert.match(p.node("text-cards-error").textContent, /UTF-8/);
  assert.equal(p.requests.length, 0);
});

test("leaving during issuance never exposes a late response", async () => {
  let resolve;
  const p = page(() => new Promise((done) => { resolve = done; }));
  p.node("text-cards-text").value = "private";
  const issuing = p.node("text-cards-form").emit("submit");
  p.control.dispose();
  resolve({ codes: ["STALE"], items: [] });
  await issuing;
  assert.equal(p.root.innerHTML, "");
  assert.equal(p.node("text-cards-generated"), undefined);
});
