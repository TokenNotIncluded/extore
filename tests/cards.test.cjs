const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../extore/static/cards.js"),
  "utf8",
);
const settle = async () => {
  for (let index = 0; index < 6; index++)
    await new Promise((resolve) => setImmediate(resolve));
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
};

function fixture(handler) {
  const nodes = new Map();
  const requests = [];
  let confirmation = true;
  class Node {
    constructor(tag = "div", attributes = "") {
      this.tagName = tag;
      this.value = attributes.match(/\bvalue="([^"]*)"/)?.[1] || "";
      this.disabled = /\bdisabled(?:\s|$)/.test(attributes);
      this.isConnected = true;
      this.dataset = {};
      this.textContent = "";
      this.listeners = new Map();
      this.children = [];
      for (const [, key, value] of attributes.matchAll(
        /\bdata-([a-z-]+)="([^"]*)"/g,
      ))
        this.dataset[
          key.replace(/-([a-z])/g, (_all, char) => char.toUpperCase())
        ] = value;
    }
    set innerHTML(value) {
      this.html = value;
      this.children = [];
      for (const [, tag, attributes] of value.matchAll(
        /<([a-z]+)\b([^>]*)>/g,
      )) {
        const element = new Node(tag, attributes);
        const id = attributes.match(/\bid="([^"]*)"/)?.[1];
        if (id) nodes.set("#" + id, element);
        this.children.push(element);
      }
    }
    get innerHTML() {
      return this.html || "";
    }
    querySelector(selector) {
      return nodes.get(selector);
    }
    querySelectorAll(selector) {
      if (selector === "input, select, button")
        return [...new Set([...nodes.values(), ...this.children])].filter(
          (node) => ["input", "select", "button"].includes(node.tagName),
        );
      const key = selector
        .match(/^\[data-([a-z-]+)\]$/)?.[1]
        .replace(/-([a-z])/g, (_all, char) => char.toUpperCase());
      return this.children.filter((node) => node.dataset[key] !== undefined);
    }
    addEventListener(event, callback) {
      this.listeners.set(event, callback);
    }
    fire(event) {
      this.listeners.get(event)?.({ preventDefault() {} });
    }
    scrollIntoView() {}
    click() {
      this.fire("click");
    }
  }
  const workspace = new Node();
  const context = vm.createContext({
    window: {
      confirm: () => confirmation,
    },
    document: { createElement: (tag) => new Node(tag) },
    AbortController,
    Blob,
    URL,
    URLSearchParams,
    Date,
    setTimeout,
  });
  vm.runInContext(source, context);
  const api = (url, body, method, options) => {
    const request = { url, body, method, options };
    requests.push(request);
    if (handler) return handler(request);
    if (url.includes("card-stats"))
      return Promise.resolve({
        summary: {
          total: 12,
          remaining: 8,
          used: 4,
          in_progress: 2,
          states: { failed_retryable: 1 },
        },
      });
    if (url.includes("card-inventory"))
      return Promise.resolve({ items: [], total: 0 });
    return Promise.resolve({});
  };
  const options = {
    api,
    workspace,
    products: [
      { id: "product-a", name: "<商品 A>" },
      { id: "product-b", name: "商品 B" },
    ],
    role: "admin",
    productId: "product-a",
  };
  return {
    nodes,
    workspace,
    requests,
    options,
    module: context.window.ExtoreCards,
    setConfirmation(value) {
      confirmation = value;
    },
  };
}

test("商品统计使用固定商品 scope，商品名称正确转义", async () => {
  const page = fixture();
  await page.module.render(page.options);
  assert.equal(page.requests.length, 2);
  assert.ok(
    page.requests.every((request) => request.url.includes("product-a")),
  );
  assert.match(page.workspace.innerHTML, /&lt;商品 A&gt;/);
  const overview = page.nodes.get("#cards-stats").innerHTML;
  assert.match(overview, /剩余未兑换/);
  assert.match(overview, /不计入未兑换数量/);
  assert.match(overview, /<h2>8<\/h2>/);
});

test("有卡密权限的商品管理链接使用 manage scope 并固定商品", async () => {
  const page = fixture();
  await page.module.render({
    ...page.options,
    role: "staff",
    products: [page.options.products[1]],
    productId: "product-b",
  });
  assert.ok(
    page.requests.every((request) => request.url.startsWith("/manage/")),
  );
  assert.ok(
    page.requests.every((request) => request.url.includes("product-b")),
  );
  assert.equal(page.nodes.get("#cards-product").disabled, true);
});

test("切商品后迟到的旧数据与错误都不能覆盖当前商品", async () => {
  const oldStats = deferred();
  const oldInventory = deferred();
  const page = fixture(({ url }) => {
    if (url.includes("product-a"))
      return url.includes("card-stats")
        ? oldStats.promise
        : oldInventory.promise;
    return Promise.resolve(
      url.includes("card-stats")
        ? { summary: { total: 99, remaining: 99 } }
        : { items: [], total: 0 },
    );
  });
  const initial = page.module.render(page.options);
  page.nodes.get("#cards-product").value = "product-b";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.match(page.nodes.get("#cards-stats").innerHTML, /<h2>99<\/h2>/);
  oldStats.reject(new Error("旧商品错误"));
  oldInventory.resolve({ items: [{ id: "old-product-card" }], total: 1 });
  await initial;
  assert.doesNotMatch(
    page.nodes.get("#cards-inventory").innerHTML,
    /old-product/,
  );
  assert.equal(page.nodes.get("#cards-error").textContent, "");
  assert.equal(page.requests[0].options.signal.aborted, true);
});

test("离开卡密页后迟到响应不改写页面", async () => {
  const stats = deferred();
  const inventory = deferred();
  const page = fixture(({ url }) =>
    url.includes("card-stats") ? stats.promise : inventory.promise,
  );
  let current = true;
  const initial = page.module.render({
    ...page.options,
    isCurrent: () => current,
  });
  current = false;
  page.workspace.innerHTML = "另一个页面";
  stats.resolve({ summary: { total: 42 } });
  inventory.resolve({ items: [], total: 0 });
  await initial;
  assert.equal(page.workspace.innerHTML, "另一个页面");
  assert.equal(page.nodes.get("#cards-stats").innerHTML, "");
});

test("库存与历史忽略 digest、完整卡密、用户输入和交付内容", async () => {
  const secrets = {
    digest: "NEVER-SHOW-DIGEST",
    code: "NEVER-SHOW-FULL-CODE",
    params: { token: "NEVER-SHOW-CUSTOMER-INPUT" },
    content: "NEVER-SHOW-DELIVERY",
  };
  const card = {
    id: "card-1",
    product_name: "商品",
    status: "unused",
    code_suffix: "ABC123",
    created: 1700000000,
    ...secrets,
  };
  const page = fixture(({ url }) => {
    if (url.includes("card-stats")) return Promise.resolve({ summary: {} });
    if (url.includes("card-inventory"))
      return Promise.resolve({ items: [card], total: 1 });
    return Promise.resolve({
      card,
      timeline: [{ type: "card.verified", created: 1700000100, ...secrets }],
    });
  });
  await page.module.render(page.options);
  page.nodes
    .get("#cards-inventory")
    .querySelectorAll("[data-card-history]")[0]
    .click();
  await settle();
  const visible =
    page.nodes.get("#cards-inventory").innerHTML +
    page.nodes.get("#cards-history").innerHTML;
  assert.match(visible, /ABC123/);
  assert.match(visible, /首次验码/);
  assert.doesNotMatch(visible, /NEVER-SHOW/);
});

test("筛选 URL 转义批次与尾号，分页始终保留商品 scope", async () => {
  const page = fixture();
  await page.module.render(page.options);
  page.nodes.get("#cards-status").value = "failed_retryable";
  page.nodes.get("#cards-batch").value = "批次 & one";
  page.nodes.get("#cards-search").value = "ABC123";
  page.nodes.get("#cards-filter").fire("submit");
  await settle();
  const request = page.requests.findLast((item) =>
    item.url.includes("card-inventory"),
  );
  const query = new URL("https://example.test" + request.url).searchParams;
  assert.equal(query.get("product_id"), "product-a");
  assert.equal(query.get("status"), "failed_retryable");
  assert.equal(query.get("batch_id"), "批次 & one");
  assert.equal(query.get("search"), "ABC123");
  assert.equal(query.get("offset"), "0");
});

test("发行原文只出现在一次生成区域，刷新库存保留下载区", async () => {
  const page = fixture(({ url }) => {
    if (url.includes("card-stats")) return Promise.resolve({ summary: {} });
    if (url.includes("card-inventory"))
      return Promise.resolve({ items: [], total: 0 });
    return Promise.resolve({ codes: ["ONCE-ONLY-CARD"] });
  });
  await page.module.render(page.options);
  page.nodes.get("#cards-count").value = "2";
  page.nodes.get("#cards-label").value = "  秋季  ";
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  const issued = page.requests.find((request) => request.method === "POST");
  assert.equal(issued.body.product_id, "product-a");
  assert.equal(issued.body.count, 2);
  assert.equal(issued.body.label, "秋季");
  assert.equal(issued.body.expires, null);
  assert.equal(page.nodes.get("#cards-generated").value, "ONCE-ONLY-CARD");
  page.nodes.get("#cards-refresh").click();
  await settle();
  assert.equal(page.nodes.get("#cards-generated").value, "ONCE-ONLY-CARD");
  assert.doesNotMatch(
    page.nodes.get("#cards-inventory").innerHTML,
    /ONCE-ONLY/,
  );
});

test("撤销必须确认，取消时不发送请求", async () => {
  const page = fixture(({ url }) =>
    Promise.resolve(
      url.includes("card-stats")
        ? { summary: {} }
        : url.includes("card-inventory")
          ? { items: [{ id: "card-1", status: "unused" }], total: 1 }
          : {},
    ),
  );
  await page.module.render(page.options);
  page.setConfirmation(false);
  page.nodes
    .get("#cards-inventory")
    .querySelectorAll("[data-card-revoke]")[0]
    .click();
  await settle();
  assert.equal(
    page.requests.filter((item) => item.method === "POST").length,
    0,
  );
  assert.equal(page.nodes.get("#cards-product").disabled, false);
});
