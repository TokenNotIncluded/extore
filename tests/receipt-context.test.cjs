const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs
  .readFileSync(path.join(__dirname, "../extore/static/app.js"), "utf8")
  .replace(/start\(\);\s*$/, "");

function page() {
  const nodes = new Map();
  const requests = [];
  const timers = new Map();
  let nextTimer = 0;
  let adapter;
  function node(selector) {
    if (!nodes.has(selector)) {
      nodes.set(selector, {
        innerHTML: "",
        textContent: "",
        value: "",
        style: {},
        isConnected: true,
        addEventListener() {},
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

  const activeDestroy = p.context.destroyReceipt();
  p.requests[1].respond(result);
  for (let i = 0; i < 10 && p.requests.length < 3; i++) await Promise.resolve();
  assert.equal(p.requests[2].body.token, "tokenB");
  p.requests[2].reject(new Error("Offline"));
  assert.equal(await activeDestroy, result);
  assert.match(p.node("#toast").textContent, /已销毁/);
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
