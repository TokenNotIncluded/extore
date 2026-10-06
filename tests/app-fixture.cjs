const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { File } = require("node:buffer");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/app.js"), "utf8").replace(/start\(\);\s*$/, "");
const flush = async () => { for (let index = 0; index < 12; index++) await Promise.resolve(); };

function appFixture({ uploadLimit = 20 * 1024 * 1024 } = {}) {
  const nodes = new Map();
  const collections = new Map();
  const requests = [];
  const timers = new Map();
  const downloads = [];
  const actions = [];
  let adapter;
  let timerId = 0;
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: "", textContent: "", value: "", style: {}, attributes: {},
      isConnected: true, checked: false, files: [], listeners: new Map(),
      addEventListener(event, callback) { this.listeners.set(event, callback); },
      async emit(event, extra = {}) {
        const offset = actions.length;
        const result = this.listeners.get(event)?.({ preventDefault() {}, target: this, ...extra });
        await Promise.all([result, ...actions.slice(offset)]);
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      remove() { this.removed = true; },
      focus() { this.focused = true; },
      select() { this.selected = true; },
      reportValidity() { return true; },
      click() { downloads.push({ href: this.href, filename: this.download }); },
    });
    return nodes.get(selector);
  };
  const location = { pathname: "/receipt", hash: "#batch-token", origin: "https://example.test" };
  const navigate = (url) => {
    const parsed = new URL(url, location.origin);
    location.pathname = parsed.pathname;
    location.hash = parsed.hash;
  };
  class FixtureURL extends URL {
    static createObjectURL() { return "blob:test-download"; }
    static revokeObjectURL() {}
  }
  const context = vm.createContext({
    document: {
      hidden: false,
      querySelector: node,
      getElementById: (id) => node("#" + id),
      querySelectorAll: (selector) => collections.get(selector) || [],
      createElement: (tag) => node("created:" + tag),
    },
    window: {
      addEventListener() {},
      ExtorePreferences: {
        resolved: { language: "zh-CN" }, settings: { language: "auto", theme: "auto" },
        subscribe() {}, setLanguage() {}, setTheme() {},
      },
      ExtoreWebMCP: { configure(value) { adapter = value; }, refresh() {} },
    },
    location,
    history: { pushState(_state, _title, url) { navigate(url); }, replaceState(_state, _title, url) { navigate(url); } },
    navigator: {}, localStorage: { getItem() { return null; }, setItem() {} },
    DOMPurify: { sanitize: (value) => value }, marked: { parse: (value) => value },
    URL: FixtureURL, URLSearchParams, FormData, File, Blob, Uint8Array,
    atob, btoa, confirm: () => true,
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      if (url === "/api/upload-limits" && uploadLimit !== null)
        return Promise.resolve({ ok: true, json: async () => ({ max_file_bytes: uploadLimit }) });
      return new Promise((resolve, reject) => {
        const body = options.body instanceof FormData
          ? Object.fromEntries(options.body.entries())
          : options.body ? JSON.parse(options.body) : undefined;
        requests.push({ url, options, body, reject,
          respond(data, blob = new Blob(["attachment"])) {
            resolve({ ok: true, json: async () => data, blob: async () => blob });
          },
        });
      });
    },
  });
  vm.runInContext(source, context);
  const perform = context.perform;
  context.perform = (...args) => { const result = perform(...args); actions.push(result); return result; };
  const set = (values) => {
    context.fixtureValues = values;
    for (const name of Object.keys(values)) vm.runInContext(`${name} = fixtureValues[${JSON.stringify(name)}]`, context);
  };
  return { context, node, requests, collections, timers, downloads, navigate, set, getContext: () => adapter.getContext() };
}

const parameter = (key, type = "text") => ({ key, type, label: { "zh-CN": key }, description: {}, required: true, collapsed: true });
const product = (name = "Product", parameters = []) => ({ id: "product", name, description: "", parameters, outputs: [parameter("content", "textarea")], view_policy: "repeat", mode: "manual", delivery: "content" });
const job = (id, state = "succeeded") => ({ id, state, delivery: "content", view_policy: "repeat", progress: 100, message: "", queue_ahead: 0, can_retry: state === "failed", attempt: 1 });
const item = (id, definition, task = job(id)) => ({ card_id: id, suffix: id, product: definition, job: task });
function batch(page, items, top = product("Current product")) {
  const data = { batch: true, product: top, items };
  page.set({ currentToken: "batch-token", currentBatch: data, currentProduct: top, batchSelection: "", batchRetryOnly: false });
  return data;
}

module.exports = { appFixture, flush, parameter, product, job, item, batch };
