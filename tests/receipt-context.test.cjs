const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs
  .readFileSync(path.join(__dirname, "../extore/static/app.js"), "utf8")
  .replace(/start\(\);\s*$/, "");

function page(language = "zh-CN") {
  const nodes = new Map();
  const requests = [];
  const timers = new Map();
  const preferenceListeners = new Set();
  const preferenceSettings = { theme: "auto", language: "auto" };
  const preferenceResolved = { theme: "light", language };
  const notifyPreferences = () => {
    for (const listener of preferenceListeners)
      listener({
        settings: { ...preferenceSettings },
        resolved: { ...preferenceResolved },
      });
  };
  const preferences = {
    get settings() {
      return { ...preferenceSettings };
    },
    get resolved() {
      return { ...preferenceResolved };
    },
    setTheme(value) {
      if (preferenceSettings.theme === value) return;
      preferenceSettings.theme = value;
      preferenceResolved.theme = value === "auto" ? "light" : value;
      notifyPreferences();
    },
    setLanguage(value) {
      if (preferenceSettings.language === value) return;
      preferenceSettings.language = value;
      preferenceResolved.language = value === "auto" ? "zh-CN" : value;
      notifyPreferences();
    },
    subscribe(callback) {
      preferenceListeners.add(callback);
      return () => preferenceListeners.delete(callback);
    },
  };
  let nextTimer = 0;
  let adapter;
  function node(selector) {
    if (!nodes.has(selector)) {
      nodes.set(selector, {
        innerHTML: "",
        textContent: "",
        value: "",
        style: {},
        attributes: {},
        isConnected: true,
        addEventListener() {},
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        remove() {
          this.removed = true;
        },
      });
    }
    return nodes.get(selector);
  }
  const location = {
    pathname: "/",
    hash: "",
    origin: "http://localhost:8000",
  };
  const navigate = (url) => {
    const target = new URL(url, location.origin);
    location.pathname = target.pathname;
    location.hash = target.hash;
  };
  const context = vm.createContext({
    document: { querySelector: node, querySelectorAll: () => [] },
    window: {
      addEventListener() {},
      ExtorePreferences: preferences,
      ExtoreWebMCP: {
        configure(value) {
          adapter = value;
        },
        refresh() {},
      },
    },
    location,
    history: {
      pushState(_state, _title, url) {
        navigate(url);
      },
      replaceState(_state, _title, url) {
        navigate(url);
      },
    },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: {},
    DOMPurify: { sanitize: (value) => value },
    marked: { parse: (value) => value },
    URLSearchParams,
    setTimeout(callback) {
      const id = ++nextTimer;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        requests.push({
          url,
          options,
          body: options.body ? JSON.parse(options.body) : undefined,
          respond(data) {
            resolve({ ok: true, json: async () => data });
          },
          reject,
        });
      });
    },
  });
  vm.runInContext(source, context);
  function receipt(token, name = token) {
    navigate("/receipt#" + token);
    const product = {
      id: name,
      name,
      parameters: [],
      description: "",
      view_policy: "repeat",
    };
    context.testReceiptToken = token;
    context.testReceiptProduct = product;
    vm.runInContext(
      "currentToken = testReceiptToken; currentProduct = testReceiptProduct",
      context,
    );
    node("#app").innerHTML = "Current receipt: " + name;
    node("#content").innerHTML = "Current content: " + name;
    return product;
  }
  return {
    context,
    node,
    navigate,
    receipt,
    requests,
    timers,
    preferences,
    getContext: () => adapter.getContext(),
  };
}

function job(state = "succeeded") {
  return {
    state,
    delivery: "content",
    view_policy: "repeat",
    progress: state === "succeeded" ? 100 : 0,
    queue_ahead: 0,
    message: "",
    can_retry: false,
  };
}

test("receipt reads cannot replace a different token or a different route", async () => {
  for (const changePage of [
    (p) => p.receipt("tokenB", "Product B"),
    (p) => p.navigate("/admin"),
    (p) => p.navigate("/receipt#tokenB"),
  ]) {
    const p = page();
    const controller = new AbortController();
    const product = p.receipt("tokenA", "Product A");
    const result = { product, job: job() };
    const reading = p.context.readReceipt({ signal: controller.signal });
    assert.equal(p.requests[0].body.token, "tokenA");
    assert.equal(p.requests[0].options.signal, controller.signal);
    changePage(p);
    const markup = p.node("#app").innerHTML;
    const selectedProduct = p.getContext().product;
    p.requests[0].respond(result);
    assert.equal(await reading, result);
    assert.equal(p.node("#app").innerHTML, markup);
    assert.equal(p.getContext().product, selectedProduct);
  }

  const p = page();
  const product = p.receipt("tokenB", "Product B");
  const reading = p.context.readReceipt();
  p.requests[0].respond({ product, job: job() });
  await reading;
  assert.match(p.node("#app").innerHTML, /Product B/);
  assert.match(p.node("#app").innerHTML, /兑换进度/);
});

test("a delayed reveal preserves the new receipt and returns successful content", async () => {
  const p = page();
  const controller = new AbortController();
  p.receipt("tokenA", "Product A");
  const revealing = p.context.revealReceipt({ signal: controller.signal });
  assert.equal(p.requests[0].body.token, "tokenA");
  assert.equal(p.requests[0].options.signal, controller.signal);
  p.receipt("tokenB", "Product B");
  const result = { content: "Private delivery for A" };
  p.requests[0].respond(result);
  assert.equal(await revealing, result);
  assert.equal(p.node("#content").innerHTML, "Current content: Product B");

  const activeReveal = p.context.revealReceipt();
  p.requests[1].respond({ content: "Private delivery for B" });
  await activeReveal;
  assert.match(p.node("#content").innerHTML, /Private delivery for B/);
});

test("a submitted redemption keeps its original token without overwriting a new page", async () => {
  const p = page();
  const controller = new AbortController();
  p.receipt("tokenA", "Product A");
  const submitting = p.context.submitRedemption(
    { email: "buyer@example.com" },
    { signal: controller.signal },
  );
  assert.deepEqual(p.requests[0].body, {
    token: "tokenA",
    params: { email: "buyer@example.com" },
  });
  assert.equal(p.requests[0].options.signal, controller.signal);
  p.receipt("tokenB", "Product B");
  const result = job("queued");
  p.requests[0].respond(result);
  assert.equal(await submitting, result);
  assert.equal(p.node("#app").innerHTML, "Current receipt: Product B");
});

test("card verification only navigates when its starting page is still active", async () => {
  const p = page();
  const controller = new AbortController();
  const verifying = p.context.exchangeCode("CARD-A", {
    signal: controller.signal,
  });
  assert.equal(p.requests[0].options.signal, controller.signal);
  p.navigate("/admin");
  p.node("#app").innerHTML = "Merchant login";
  const result = {
    token: "tokenA",
    product: { id: "A", name: "A", parameters: [], description: "" },
  };
  p.requests[0].respond(result);
  assert.equal(await verifying, result);
  assert.equal(p.getContext().page, "admin");
  assert.equal(p.getContext().currentToken, "");
  assert.equal(p.node("#app").innerHTML, "Merchant login");

  p.navigate("/");
  const activeVerification = p.context.exchangeCode("CARD-A");
  p.requests[1].respond(result);
  await activeVerification;
  assert.equal(p.getContext().currentToken, "tokenA");
  assert.equal(p.getContext().page, "receipt");
  assert.match(p.node("#app").innerHTML, /确认兑换信息/);
});

test("successful destruction does not refresh another receipt or report refresh failure as destruction failure", async () => {
  const p = page();
  const controller = new AbortController();
  p.receipt("tokenA", "Product A");
  const destroying = p.context.destroyReceipt({ signal: controller.signal });
  assert.equal(p.requests[0].body.token, "tokenA");
  assert.equal(p.requests[0].options.signal, controller.signal);
  p.receipt("tokenB", "Product B");
  const result = { ok: true };
  p.requests[0].respond(result);
  assert.equal(await destroying, result);
  assert.equal(p.requests.length, 1);
  assert.equal(p.node("#app").innerHTML, "Current receipt: Product B");
  assert.equal(p.node("#content").innerHTML, "Current content: Product B");
  assert.notEqual(p.node("#reveal").removed, true);
  assert.notEqual(p.node("#destroy").removed, true);

  p.node("#content").innerHTML = "Visible private delivery for B";
  const activeDestroy = p.context.destroyReceipt();
  p.requests[1].respond(result);
  for (let i = 0; i < 10 && p.requests.length < 3; i++) await Promise.resolve();
  assert.equal(p.requests[2].body.token, "tokenB");
  // The successful delete must clear visible secrets before any refresh finishes.
  assert.equal(p.node("#content").innerHTML, "");
  assert.equal(p.node("#reveal").removed, true);
  assert.equal(p.node("#destroy").removed, true);
  p.requests[2].reject(new Error("Offline"));
  assert.equal(await activeDestroy, result);
  assert.equal(p.node("#content").innerHTML, "");
  assert.match(p.node("#toast").textContent, /已销毁/);
});

test("destruction invalidates earlier reveal and receipt responses while refreshing", async () => {
  const p = page();
  const product = p.receipt("tokenA", "Product A");
  const reading = p.context.readReceipt();
  const revealing = p.context.revealReceipt();
  const destroying = p.context.destroyReceipt();
  p.requests[2].respond({ ok: true });
  for (let i = 0; i < 10 && p.requests.length < 4; i++) await Promise.resolve();
  assert.equal(p.requests[3].url, "/api/receipt");
  assert.equal(p.node("#content").innerHTML, "");
  const markup = p.node("#app").innerHTML;

  p.requests[0].respond({
    product: { ...product, name: "Stale product name" },
    job: job(),
  });
  await reading;
  p.requests[1].respond({ output: { content: "Destroyed private delivery" } });
  await revealing;
  assert.equal(p.node("#app").innerHTML, markup);
  assert.equal(p.node("#content").innerHTML, "");
  assert.equal(p.getContext().product, product);
  assert.equal(p.node("#reveal").removed, true);

  p.requests[3].respond({ product, job: job("destroyed") });
  assert.deepEqual(await destroying, { ok: true });
  assert.match(p.node("#app").innerHTML, /已销毁/);
});

test("pre-destruction responses cannot restore content after the refresh fails", async () => {
  const p = page();
  const product = p.receipt("tokenA", "Product A");
  const revealing = p.context.revealReceipt();
  const reading = p.context.readReceipt();
  const destroying = p.context.destroyReceipt();
  p.requests[2].respond({ ok: true });
  for (let i = 0; i < 10 && p.requests.length < 4; i++) await Promise.resolve();
  p.requests[3].reject(new Error("Offline"));
  assert.deepEqual(await destroying, { ok: true });
  const markup = p.node("#app").innerHTML;
  assert.match(p.node("#toast").textContent, /已销毁/);

  p.requests[0].respond({ content: "Destroyed legacy private delivery" });
  await revealing;
  p.requests[1].respond({ product, job: job() });
  await reading;
  assert.equal(p.node("#app").innerHTML, markup);
  assert.equal(p.node("#content").innerHTML, "");
  assert.equal(p.node("#reveal").removed, true);
  assert.equal(p.node("#destroy").removed, true);
});

test("failed destruction preserves an earlier pending reveal", async () => {
  const p = page();
  p.receipt("tokenA", "Product A");
  const revealing = p.context.revealReceipt();
  const destroying = p.context.destroyReceipt();
  p.requests[1].reject(new Error("Offline"));
  await assert.rejects(destroying, /Offline/);
  p.requests[0].respond({ output: { content: "Still available delivery" } });
  await revealing;
  assert.match(p.node("#content").innerHTML, /Still available delivery/);
  assert.notEqual(p.node("#reveal").removed, true);
});

test("structured delivery displays localized output labels and escapes all returned text", async () => {
  for (const [language, expectedLabel] of [
    ["zh-CN", "领取账号 &lt;img src=x onerror=alert(1)&gt;"],
    ["en", "Account &lt;img src=x onerror=alert(1)&gt;"],
  ]) {
    const p = page(language);
    const product = p.receipt("tokenA", "Product A");
    product.outputs = [
      {
        key: "email",
        label: {
          "zh-CN": "领取账号 <img src=x onerror=alert(1)>",
          en: "Account <img src=x onerror=alert(1)>",
        },
      },
      {
        key: "access_code",
        label: { "zh-CN": "访问代码", en: "Access code" },
      },
    ];
    const result = {
      output: {
        email: "buyer@example.com",
        access_code: '</pre><script>alert("delivery")</script>',
        '<svg onload="alert(2)">': "Fallback output",
      },
    };
    const revealing = p.context.revealReceipt();
    p.requests[0].respond(result);
    assert.equal(await revealing, result);
    const markup = p.node("#content").innerHTML;
    assert.ok(markup.includes("<h3>" + expectedLabel + "</h3>"));
    assert.match(markup, /buyer@example\.com/);
    assert.match(markup, /&lt;\/pre&gt;&lt;script&gt;alert\(&quot;delivery&quot;\)&lt;\/script&gt;/);
    assert.match(markup, /&lt;svg onload=&quot;alert\(2\)&quot;&gt;/);
    assert.match(markup, /Fallback output/);
    assert.doesNotMatch(markup, /<(?:script|img|svg)\b/i);
  }
});

test("polling ignores stale timers and late responses from another receipt", async () => {
  const p = page();
  p.receipt("tokenA", "Product A");
  p.context.renderReceipt(job("queued"));
  const staleTimer = [...p.timers.values()][0];
  p.receipt("tokenB", "Product B");
  await staleTimer();
  assert.equal(p.requests.length, 0);

  p.receipt("tokenA", "Product A");
  p.context.renderReceipt(job("queued"));
  const polling = [...p.timers.values()][0]();
  assert.equal(p.requests[0].body.token, "tokenA");
  p.receipt("tokenB", "Product B");
  p.requests[0].respond({
    product: { id: "A", name: "Product A", parameters: [] },
    job: job(),
  });
  await polling;
  assert.equal(p.node("#app").innerHTML, "Current receipt: Product B");
  assert.equal(p.getContext().product.name, "Product B");
});

test("changing the appearance preserves the receipt and its pending delivery", async () => {
  const p = page();
  const product = p.receipt("tokenA", "Product A");
  const revealing = p.context.revealReceipt();
  p.preferences.setTheme("dark");
  assert.equal(p.requests.length, 1);
  assert.equal(p.getContext().currentToken, "tokenA");
  assert.equal(p.getContext().product, product);
  assert.equal(p.node("#app").innerHTML, "Current receipt: Product A");
  assert.equal(p.node("#theme").value, "dark");
  p.requests[0].respond({ content: "Private delivery for A" });
  await revealing;
  assert.match(p.node("#content").innerHTML, /Private delivery for A/);
});

test("changing the language reloads the same receipt and updates its controls", async () => {
  const p = page();
  const product = p.receipt("tokenA", "Product A");
  p.preferences.setLanguage("en");
  assert.equal(p.requests[0].url, "/api/auth/status");
  assert.equal(p.node("#language").value, "en");
  assert.equal(p.node("#language").attributes["aria-label"], "Language");
  p.requests[0].respond({ configured: true, role: null, password_enabled: false });
  for (let i = 0; i < 10 && p.requests.length < 2; i++) await Promise.resolve();
  assert.equal(p.requests[1].url, "/api/receipt");
  assert.equal(p.requests[1].body.token, "tokenA");
  p.requests[1].respond({ product, job: job() });
  for (let i = 0; i < 10; i++) await Promise.resolve();
  assert.equal(p.getContext().currentToken, "tokenA");
  assert.equal(p.getContext().product, product);
  assert.match(p.node("#app").innerHTML, /Redemption progress/);
});
