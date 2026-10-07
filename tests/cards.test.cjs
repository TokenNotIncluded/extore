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

function fixture(handler, { language = "zh-CN", batches = [{ id: "fixture-batch", label: "十月发行", total: 12, remaining: 8, used: 4 }] } = {}) {
  const nodes = new Map();
  const requests = [];
  const copied = [];
  const created = [];
  const clipboard = {
    async writeText(value) {
      copied.push(value);
    },
  };
  let confirmation = true;
  const decodeAttribute = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (_all, entity) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[entity]);
  class Node {
    constructor(tag = "div", attributes = "") {
      this.tagName = tag;
      this.value = decodeAttribute(attributes.match(/\bvalue="([^"]*)"/)?.[1] || "");
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
        ] = decodeAttribute(value);
    }
    set innerHTML(value) {
      this.html = value;
      this.children = [];
      for (const [, tag, attributes] of value.matchAll(
        /<([a-z][a-z0-9-]*)\b([^>]*)>/g,
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
      if (["input, select, button", "input, select, textarea, button"].includes(selector))
        return [...new Set([...nodes.values(), ...this.children])].filter(
          (node) => ["input", "select", "textarea", "button"].includes(node.tagName),
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
    focus() {
      this.focused = true;
    }
    select() {
      this.selected = true;
    }
    setSelectionRange(start, end) {
      this.selectionRange = [start, end];
    }
    click() {
      this.fire("click");
    }
  }
  const workspace = new Node();
  const context = vm.createContext({
    window: {
      confirm: () => confirmation,
      ExtorePreferences: { resolved: { language } },
    },
    document: {
      createElement(tag) {
        const element = new Node(tag);
        created.push(element);
        return element;
      },
    },
    navigator: { clipboard },
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
    if (/\/card-batches\?/.test(url))
      return Promise.resolve(typeof batches === "function" ? batches(request) : { items: batches, total: batches.length });
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
    copied,
    created,
    clipboard,
    options,
    module: context.window.ExtoreCards,
    setConfirmation(value) {
      confirmation = value;
    },
    setClipboardHelper(helper) {
      context.window.ExtoreClipboard = helper;
    },
    setTextCards(helper) {
      context.window.ExtoreTextCards = helper;
    },
  };
}

async function openFirstBatch(page) {
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch]")[0].click();
  await settle();
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

test("历史商品保留在选择器中，默认优先选择未删除商品", async () => {
  const page = fixture();
  const instance = await page.module.render({
    ...page.options,
    productId: undefined,
    products: [
      { id: "deleted-product", name: "<历史商品>", deleted: true, deleted_at: 1700000000 },
      page.options.products[1],
    ],
  });
  assert.equal(instance.productId, "product-b");
  assert.ok(page.requests.every((request) => request.url.includes("product-b")));
  assert.match(page.workspace.innerHTML, /&lt;历史商品&gt; · 已删除/);
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, false);
  assert.equal(page.nodes.get("#cards-product-notice").textContent, "");
});

test("purged 商品仍可查看旧卡密，但不能发行或补充库存", async () => {
  const page = fixture();
  await page.module.render({ ...page.options, products: [{ id: "product-a", name: "历史商品", purged: true, purged_at: 123 }] });
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  assert.equal(page.requests.filter((request) => request.method === "POST").length, 0);
  assert.ok(page.requests.some((request) => request.url.includes("card-batches")));
});

test("显式选择已删除商品仍可查卡密和历史，禁止发行并阻止手动提交", async () => {
  const page = fixture(({ url }) => Promise.resolve(
    url.includes("card-stats") ? { summary: { total: 1 } }
      : url.includes("card-inventory") ? { items: [{ id: "old-card", status: "unused" }], total: 1 }
        : { card: { id: "old-card", product_name: "历史商品", status: "unused" }, timeline: [] },
  ));
  const instance = await page.module.render({
    ...page.options,
    products: [{ id: "product-a", name: "历史商品", deleted: true }],
  });
  assert.equal(instance.productId, "product-a");
  for (const selector of ["#cards-issue-variant", "#cards-issue-submit", "#cards-count", "#cards-label", "#cards-expires"])
    assert.equal(page.nodes.get(selector).disabled, true);
  assert.equal(page.nodes.get("#cards-variant").disabled, false);
  assert.match(page.nodes.get("#cards-product-notice").textContent, /已删除.*仍可查看/);
  assert.match(page.nodes.get("#cards-stats").innerHTML, /<h2>1<\/h2>/);
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  assert.equal(page.requests.filter((request) => request.method === "POST").length, 0);
  assert.match(page.nodes.get("#cards-error").textContent, /已删除商品不能生成卡密/);
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  await openFirstBatch(page);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-history]")[0].click();
  await settle();
  assert.match(page.nodes.get("#cards-history").innerHTML, /历史商品/);
  assert.equal(page.requests.at(-1).url, "/admin/cards/old-card/history?product_id=product-a");
});

test("商品切换恢复发行状态，删除商品不因刷新规格重新启用", async () => {
  const page = fixture(({ url }) => Promise.resolve(url.includes("card-stats")
    ? { summary: {}, variants: [{ variant_id: "default", name: "默认规格", enabled: true }] }
    : { items: [], total: 0 }));
  await page.module.render({
    ...page.options,
    products: [page.options.products[0], { ...page.options.products[1], deleted: true }],
  });
  page.nodes.get("#cards-product").value = "product-b";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  page.nodes.get("#cards-refresh").click();
  await settle();
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  page.nodes.get("#cards-product").value = "product-a";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, false);
  assert.equal(page.nodes.get("#cards-count").disabled, false);
  assert.equal(page.nodes.get("#cards-product-notice").textContent, "");
});

test("所有商品均删除时保留历史入口，deleted_at 也会禁止库存导入", async () => {
  const page = fixture();
  let mounts = 0;
  page.setTextCards({ mount() { mounts++; return { dispose() {} }; } });
  const instance = await page.module.render({
    ...page.options,
    productId: undefined,
    products: [{ id: "deleted-stock", name: "历史库存", mode: "stock", deleted_at: 1700000000 }],
  });
  assert.equal(instance.productId, "deleted-stock");
  assert.ok(page.requests.every((request) => request.url.includes("deleted-stock")));
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  assert.equal(mounts, 0);
  assert.match(page.nodes.get("#cards-product-notice").textContent, /不能再发行卡密或补充库存/);
});

test("切换到删除的库存商品先销毁旧导入表单，切回正常商品才重新加载", async () => {
  const page = fixture();
  const mounts = [];
  let disposals = 0;
  page.setTextCards({
    mount({ root, product }) {
      mounts.push(product.id);
      root.innerHTML = '<input id="old-stock-input">';
      return { dispose() { disposals++; } };
    },
  });
  await page.module.render({
    ...page.options,
    products: [
      { ...page.options.products[0], mode: "stock" },
      { ...page.options.products[1], mode: "stock", deleted: true },
    ],
  });
  assert.deepEqual(mounts, ["product-a"]);
  page.nodes.get("#cards-product").value = "product-b";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.equal(disposals, 1);
  assert.deepEqual(mounts, ["product-a"]);
  assert.equal(page.nodes.get("#cards-text-import").innerHTML, "");
  page.nodes.get("#cards-product").value = "product-a";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.deepEqual(mounts, ["product-a", "product-a"]);
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
  await openFirstBatch(page);
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
  const page = fixture(undefined, { batches: [{ id: "批次 & one", label: "带符号的批次", total: 1 }] });
  await page.module.render(page.options);
  await openFirstBatch(page);
  page.nodes.get("#cards-status").value = "failed_retryable";
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
  await openFirstBatch(page);
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

const issueResponse = ({ url }) =>
  Promise.resolve(
    url.includes("card-stats")
      ? { summary: {} }
      : url.includes("card-inventory")
        ? { items: [], total: 0 }
        : { codes: ["FIRST-CARD-ABC123", "SECOND-CARD-DEF456"] },
  );
async function issue(page) {
  await page.module.render(page.options);
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
}

test("可一键复制整批或选中的单条，只用本次发行返回的原文", async () => {
  const page = fixture(issueResponse);
  await issue(page);
  page.nodes.get("#cards-copy-all").click();
  await settle();
  assert.deepEqual(page.copied, ["FIRST-CARD-ABC123\nSECOND-CARD-DEF456"]);
  assert.equal(
    page.nodes.get("#cards-copy-feedback").textContent,
    "已复制 2 条卡密。",
  );
  page.nodes.get("#cards-single").value = "1";
  page.nodes.get("#cards-copy-single").click();
  await settle();
  assert.equal(page.copied[1], "SECOND-CARD-DEF456");
  assert.equal(
    page.nodes.get("#cards-copy-feedback").textContent,
    "已复制第 2 条卡密。",
  );
  assert.equal(
    page.requests.filter((request) => request.method === "POST").length,
    1,
  );
});

test("英文模式提供英文复制按钮及反馈，沿用主题样式", async () => {
  const page = fixture(issueResponse, { language: "en" });
  await issue(page);
  const html = page.nodes.get("#cards-codes").innerHTML;
  assert.match(html, /class="secondary">Copy all/);
  assert.match(html, /Copy individual codes/);
  assert.match(html, /Copy this code/);
  page.nodes.get("#cards-copy-all").click();
  await settle();
  assert.equal(
    page.nodes.get("#cards-copy-feedback").textContent,
    "Copied 2 codes.",
  );
});

test("剪贴板拒绝时选中对应原文，显示失败提示并允许再次复制", async () => {
  const page = fixture(issueResponse);
  await issue(page);
  page.clipboard.writeText = async () => {
    throw new Error("Clipboard permission denied");
  };
  page.nodes.get("#cards-single").value = "1";
  page.nodes.get("#cards-copy-single").click();
  await settle();
  const generated = page.nodes.get("#cards-generated");
  assert.equal(generated.focused, true);
  assert.deepEqual(generated.selectionRange, [18, 36]);
  assert.match(page.nodes.get("#cards-copy-feedback").textContent, /复制失败/);
  assert.equal(page.nodes.get("#cards-copy-all").disabled, false);
  assert.equal(page.nodes.get("#cards-copy-single").disabled, false);
  page.clipboard.writeText = undefined;
  page.nodes.get("#cards-copy-all").click();
  await settle();
  assert.equal(generated.selected, true);
  assert.equal(page.nodes.get("#cards-copy-all").disabled, false);
});

test("切换商品后迟到的剪贴板结果不改写当前页面", async () => {
  const page = fixture(issueResponse);
  await issue(page);
  const clipboard = deferred();
  page.clipboard.writeText = () => clipboard.promise;
  page.nodes.get("#cards-copy-all").click();
  page.nodes.get("#cards-product").value = "product-b";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  page.nodes.get("#cards-copy-feedback").textContent = "当前商品提示";
  clipboard.resolve();
  await settle();
  assert.equal(
    page.nodes.get("#cards-copy-feedback").textContent,
    "当前商品提示",
  );
  assert.equal(page.nodes.get("#cards-codes").innerHTML, "");
});

test("复用统一剪贴板 helper，false 仍提供原文选择与失败反馈", async () => {
  const page = fixture(issueResponse);
  await issue(page);
  const values = [];
  let supported = false;
  page.setClipboardHelper({
    async writeText(value) {
      values.push(value);
      return supported;
    },
  });
  page.nodes.get("#cards-copy-all").click();
  await settle();
  assert.match(page.nodes.get("#cards-copy-feedback").textContent, /复制失败/);
  assert.equal(page.nodes.get("#cards-generated").selected, true);
  supported = true;
  page.nodes.get("#cards-single").value = "1";
  page.nodes.get("#cards-copy-single").click();
  await settle();
  assert.deepEqual(values, [
    "FIRST-CARD-ABC123\nSECOND-CARD-DEF456",
    "SECOND-CARD-DEF456",
  ]);
  assert.match(
    page.nodes.get("#cards-copy-feedback").textContent,
    /已复制第 2 条/,
  );
  assert.deepEqual(page.copied, []);
});

const variantProduct = (variants) => ({
  id: "product-a",
  name: "规格商品",
  variants,
});
const standardVariant = {
  id: "default",
  name: "标准版",
  description: "默认规格",
  price: "9.90",
  currency: "CNY",
  attributes: { seats: 1 },
  enabled: true,
};
const premiumVariant = {
  id: "annual-pro",
  name: "年卡 Deluxe",
  description: "年度规格",
  price: "49.90",
  currency: "CNY",
  attributes: { seats: 3 },
  enabled: true,
};
function selectMarkup(page, id) {
  const select = page.nodes.get("#" + id);
  return (
    select?.innerHTML ||
    page.workspace.innerHTML.match(
      new RegExp(`<select\\b[^>]*\\bid="${id}"[^>]*>([\\s\\S]*?)<\\/select>`),
    )?.[1] ||
    ""
  );
}
const latestInventoryQuery = (page) =>
  new URL(
    "https://example.test" +
      page.requests.findLast((request) =>
        request.url.includes("card-inventory"),
      ).url,
  ).searchParams;

test("制卡只列出可用规格，默认预选 default，并绑定明确选择的规格", async () => {
  const page = fixture(issueResponse);
  page.options.products = [
    variantProduct([
      premiumVariant,
      { ...standardVariant, enabled: false, id: "archived", name: "停售规格" },
      standardVariant,
    ]),
  ];
  await page.module.render(page.options);
  assert.equal(page.nodes.get("#cards-issue-variant").value, "default");
  const choices = selectMarkup(page, "cards-issue-variant");
  assert.match(choices, /annual-pro/);
  assert.match(choices, /default/);
  assert.doesNotMatch(choices, /archived|停售规格/);
  page.nodes.get("#cards-issue-variant").value = "annual-pro";
  page.nodes.get("#cards-issue-variant").fire("change");
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  const request = page.requests.find((item) => item.method === "POST");
  assert.equal(request.body.product_id, "product-a");
  assert.equal(request.body.variant_id, "annual-pro");
});

test("旧商品兼容 default，停用 default 后预选第一个可用规格", async () => {
  for (const [product, expected] of [
    [{ id: "product-a", name: "旧商品" }, "default"],
    [
      variantProduct([
        { ...standardVariant, enabled: false },
        premiumVariant,
        { ...premiumVariant, id: "monthly", name: "月卡" },
      ]),
      "annual-pro",
    ],
  ]) {
    const page = fixture(issueResponse);
    page.options.products = [product];
    await issue(page);
    assert.equal(page.nodes.get("#cards-issue-variant").value, expected);
    assert.equal(
      page.requests.find((request) => request.method === "POST").body
        .variant_id,
      expected,
    );
  }
});

test("全规格停用时禁止制卡，仍可筛选旧卡密并读取规格历史", async () => {
  const page = fixture(({ url }) => {
    if (url.includes("card-stats")) return Promise.resolve({ summary: {} });
    const card = {
      id: "old-card",
      variant_id: "annual-pro",
      variant_name: "停售年卡",
      status: "unused",
    };
    if (url.includes("card-inventory"))
      return Promise.resolve({ items: [card], total: 1 });
    return Promise.resolve({ card, timeline: [] });
  });
  page.options.products = [
    variantProduct([{ ...premiumVariant, name: "停售年卡", enabled: false }]),
  ];
  await page.module.render(page.options);
  assert.equal(page.nodes.get("#cards-issue-submit").disabled, true);
  assert.equal(page.nodes.get("#cards-variant").disabled, false);
  assert.match(selectMarkup(page, "cards-variant"), /annual-pro/);
  const visible =
    page.workspace.innerHTML +
    [...page.nodes.values()].map((node) => node.textContent).join(" ");
  assert.match(
    visible,
    /无可用规格|没有可用规格|暂无启用规格|没有启用.*规格|请先.*规格/,
  );
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  assert.equal(
    page.requests.filter((request) => request.method === "POST").length,
    0,
  );
  await openFirstBatch(page);
  page.nodes.get("#cards-variant").value = "annual-pro";
  page.nodes.get("#cards-filter").fire("submit");
  await settle();
  assert.equal(latestInventoryQuery(page).get("variant_id"), "annual-pro");
  page.nodes
    .get("#cards-inventory")
    .querySelectorAll("[data-card-history]")[0]
    .click();
  await settle();
  assert.match(page.nodes.get("#cards-history").innerHTML, /停售年卡/);
});

test("规格概览保留商品总数，安全呈现各规格剩余与发行量及库存历史", async () => {
  const unsafeName = '<标准 & "特别"版>';
  const escapedName = "&lt;标准 &amp; &quot;特别&quot;版&gt;";
  const card = {
    id: "variant-card",
    status: "unused",
    variant_id: "default",
    variant_name: unsafeName,
  };
  const page = fixture(({ url }) => {
    if (url.includes("card-stats"))
      return Promise.resolve({
        summary: { total: 14, remaining: 9 },
        variants: [
          {
            variant_id: "default",
            name: unsafeName,
            price: "9.90",
            currency: "CNY",
            enabled: true,
            summary: { total: 10, remaining: 8 },
          },
          {
            variant_id: "annual-pro",
            name: "年卡 Deluxe",
            price: "49.90",
            currency: "CNY",
            enabled: false,
            summary: { total: 4, remaining: 1 },
          },
        ],
      });
    if (url.includes("card-inventory"))
      return Promise.resolve({ items: [card], total: 1 });
    return Promise.resolve({ card, timeline: [] });
  });
  page.options.products = [
    variantProduct([
      { ...standardVariant, name: unsafeName },
      { ...premiumVariant, enabled: false },
    ]),
  ];
  await page.module.render(page.options);
  const overview = page.nodes.get("#cards-stats").innerHTML;
  assert.match(overview, /<h2>14<\/h2>/);
  assert.match(overview, /<h2>9<\/h2>/);
  assert.ok(overview.includes(escapedName));
  assert.match(overview, /年卡 Deluxe/);
  const text = overview.replace(/<[^>]+>/g, " ");
  assert.match(text, /\b10\b/);
  assert.match(text, /\b8\b/);
  assert.match(text, /\b4\b/);
  assert.match(text, /\b1\b/);
  await openFirstBatch(page);
  const inventory = page.nodes.get("#cards-inventory").innerHTML;
  assert.ok(inventory.includes(escapedName));
  assert.doesNotMatch(inventory + overview, /<标准/);
  page.nodes
    .get("#cards-inventory")
    .querySelectorAll("[data-card-history]")[0]
    .click();
  await settle();
  assert.ok(page.nodes.get("#cards-history").innerHTML.includes(escapedName));
});

test("库存规格筛选跨分页保留，清空后回到所有规格且不影响制卡选择", async () => {
  const page = fixture(({ url }) =>
    Promise.resolve(
      url.includes("card-stats")
        ? { summary: {} }
        : { items: [{ id: "card", status: "unused" }], total: 120 },
    ),
  );
  page.options.products = [variantProduct([standardVariant, premiumVariant])];
  await page.module.render(page.options);
  assert.equal(page.nodes.get("#cards-variant").value, "");
  await openFirstBatch(page);
  assert.equal(latestInventoryQuery(page).has("variant_id"), false);
  page.nodes.get("#cards-variant").value = "annual-pro";
  page.nodes.get("#cards-filter").fire("submit");
  await settle();
  assert.equal(latestInventoryQuery(page).get("variant_id"), "annual-pro");
  assert.equal(latestInventoryQuery(page).get("offset"), "0");
  page.nodes.get("#cards-next").click();
  await settle();
  assert.equal(latestInventoryQuery(page).get("variant_id"), "annual-pro");
  assert.equal(latestInventoryQuery(page).get("offset"), "50");
  assert.equal(latestInventoryQuery(page).get("product_id"), "product-a");
  page.nodes.get("#cards-reset").click();
  await settle();
  assert.equal(page.nodes.get("#cards-variant").value, "");
  assert.equal(latestInventoryQuery(page).has("variant_id"), false);
  assert.equal(latestInventoryQuery(page).get("offset"), "0");
  assert.equal(page.nodes.get("#cards-issue-variant").value, "default");
});

test("发行原文标题与下载文件名携带本次规格，下载不重新调用制卡接口", async () => {
  const page = fixture(issueResponse);
  page.options.products = [variantProduct([premiumVariant])];
  await issue(page);
  assert.match(page.nodes.get("#cards-codes").innerHTML, /年卡 Deluxe/);
  page.nodes.get("#cards-download").click();
  await settle();
  const download = page.created.find((element) => element.tagName === "a");
  assert.ok(download);
  assert.match(download.download, /annual-pro/);
  assert.match(download.download, /年卡[ _-]?Deluxe/);
  assert.match(download.download, /\.txt$/);
  assert.match(download.href, /^blob:/);
  assert.equal(
    page.requests.filter((request) => request.method === "POST").length,
    1,
  );
});

test("切制卡规格清除上次原文，迟到复制结果不能覆盖下一规格提示", async () => {
  const page = fixture(issueResponse);
  page.options.products = [variantProduct([standardVariant, premiumVariant])];
  await issue(page);
  const clipboard = deferred();
  page.clipboard.writeText = () => clipboard.promise;
  page.nodes.get("#cards-copy-all").click();
  const oldFeedback = page.nodes.get("#cards-copy-feedback");
  page.nodes.get("#cards-issue-variant").value = "annual-pro";
  page.nodes.get("#cards-issue-variant").fire("change");
  await settle();
  assert.equal(page.nodes.get("#cards-codes").innerHTML, "");
  oldFeedback.textContent = "下一规格的提示";
  clipboard.resolve();
  await settle();
  assert.equal(oldFeedback.textContent, "下一规格的提示");
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  assert.equal(
    page.requests.findLast((request) => request.method === "POST").body
      .variant_id,
    "annual-pro",
  );
  assert.match(page.nodes.get("#cards-codes").innerHTML, /年卡 Deluxe/);
});

test("首屏是批次文件夹，不请求或平铺全部卡密", async () => {
  const page = fixture(undefined, { batches: [{
    id: "batch-a", label: '<五月 & "特别"批次>', total: 20, remaining: 12,
    used: 8, in_progress: 2, digest: "NEVER-SHOW-DIGEST", code: "NEVER-SHOW-CODE",
    content: "NEVER-SHOW-DELIVERY", params: { secret: "NEVER-SHOW-INPUT" },
  }] });
  await page.module.render(page.options);
  assert.equal(page.requests.length, 2);
  assert.equal(page.requests.filter((request) => request.url.includes("card-inventory")).length, 0);
  const folders = page.nodes.get("#cards-inventory").innerHTML;
  assert.match(folders, /cards-folder-list/);
  assert.match(folders, /&lt;五月 &amp; &quot;特别&quot;批次&gt;/);
  assert.match(folders, /总数 20/);
  assert.match(folders, /未兑换 12/);
  assert.match(folders, /已使用 8/);
  assert.doesNotMatch(folders, /NEVER-SHOW/);
  assert.equal(page.nodes.get("#cards-back").hidden, true);
  assert.match(page.nodes.get("#cards-page").textContent, /个批次/);
});

test("打开批次后才读取该文件夹，返回批次不读取全量卡密", async () => {
  const page = fixture();
  await page.module.render(page.options);
  await openFirstBatch(page);
  assert.equal(latestInventoryQuery(page).get("batch_id"), "fixture-batch");
  assert.equal(latestInventoryQuery(page).get("product_id"), "product-a");
  assert.equal(page.nodes.get("#cards-back").hidden, false);
  assert.equal(page.nodes.get("#cards-library-title").textContent, "十月发行");
  page.nodes.get("#cards-back").click();
  await settle();
  assert.equal(page.nodes.get("#cards-back").hidden, true);
  assert.match(page.requests.at(-1).url, /\/card-batches\?/);
  assert.equal(page.requests.filter((request) => request.url.includes("card-inventory")).length, 1);
});

test("批次删除先预览，取消不删除，确认绑定服务器修订", async () => {
  const page = fixture(({ url, method }) => {
    if (url.includes("delete-preview")) return Promise.resolve({
      batch: { id: "fixture-batch", label: "十月发行", total: 12 }, revision: "revision-1",
      delete_count: 0, retain_count: 12, revocable_count: 7, in_progress: 2,
      explanation: '<保留交付 & 当前任务>',
    });
    if (method === "DELETE") return Promise.resolve({ ok: true, deleted: true });
    return Promise.resolve({ summary: {} });
  });
  await page.module.render(page.options);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  await settle();
  assert.equal(page.requests.filter((request) => request.method === "DELETE").length, 0);
  assert.equal(page.nodes.get("#cards-batch-review").hidden, false);
  assert.match(page.nodes.get("#cards-batch-review").innerHTML, /停止兑换/);
  assert.match(page.nodes.get("#cards-batch-review").innerHTML, /&lt;保留交付 &amp; 当前任务&gt;/);
  page.nodes.get("#cards-batch-cancel").click();
  await settle();
  assert.equal(page.nodes.get("#cards-batch-review").hidden, true);
  assert.equal(page.requests.filter((request) => request.method === "DELETE").length, 0);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  await settle();
  page.nodes.get("#cards-batch-confirm").click();
  await settle();
  const deletion = page.requests.find((request) => request.method === "DELETE");
  assert.equal(deletion.url, "/admin/card-batches/fixture-batch?product_id=product-a");
  assert.equal(deletion.body.revision, "revision-1");
  assert.equal(deletion.body.confirmed, true);
  assert.equal(page.nodes.get("#cards-batch-review").hidden, true);
});

test("批次视图分页和规格搜索不读取卡密，商品切换重置文件夹与回收站", async () => {
  const page = fixture(undefined, { batches: () => ({ items: [{ id: "batch-page", label: "发行批次", total: 80 }], total: 120 }) });
  page.options.products[0].variants = [standardVariant, premiumVariant];
  await page.module.render(page.options);
  page.nodes.get("#cards-variant").value = "annual-pro";
  page.nodes.get("#cards-search").value = "十月 & one";
  page.nodes.get("#cards-filter").fire("submit");
  await settle();
  page.nodes.get("#cards-next").click();
  await settle();
  const query = new URL("https://example.test" + page.requests.at(-1).url).searchParams;
  assert.equal(query.get("offset"), "50");
  assert.equal(query.get("variant_id"), "annual-pro");
  assert.equal(query.get("search"), "十月 & one");
  assert.equal(query.get("view"), "active");
  assert.equal(page.requests.filter((request) => request.url.includes("card-inventory")).length, 0);
  page.nodes.get("#cards-view").value = "deleted";
  page.nodes.get("#cards-view").fire("change");
  await settle();
  await openFirstBatch(page);
  page.nodes.get("#cards-product").value = "product-b";
  page.nodes.get("#cards-product").fire("change");
  await settle();
  assert.equal(page.nodes.get("#cards-back").hidden, true);
  assert.equal(page.nodes.get("#cards-view").value, "active");
  assert.match(page.requests.at(-1).url, /product_id=product-b/);
  assert.match(page.requests.at(-1).url, /view=active/);
});

test("回收站可恢复，永久清理只在预览确认后执行", async () => {
  const page = fixture(({ url }) => {
    if (url.includes("purge-preview")) return Promise.resolve({
      batch: { id: "fixture-batch", label: "已删批次", deleted: true }, revision: "purge-revision",
      delete_count: 5, retain_count: 7, explanation: "删除未引用卡密，保留任务与交付。",
    });
    return Promise.resolve({ summary: {} });
  }, { language: "en" });
  await page.module.render(page.options);
  page.nodes.get("#cards-view").value = "deleted";
  page.nodes.get("#cards-view").fire("change");
  await settle();
  assert.match(page.requests.at(-1).url, /view=deleted/);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-restore]")[0].click();
  await settle();
  assert.ok(page.requests.some((request) => request.url === "/admin/card-batches/fixture-batch/restore?product_id=product-a" && request.method === "POST"));
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  await settle();
  assert.match(page.nodes.get("#cards-batch-review").innerHTML, /Confirm permanent cleanup/);
  assert.equal(page.requests.filter((request) => request.url.includes("/purge?")).length, 0);
  page.nodes.get("#cards-batch-confirm").click();
  await settle();
  const purge = page.requests.find((request) => request.url.includes("/purge?"));
  assert.equal(purge.method, "POST");
  assert.equal(purge.body.revision, "purge-revision");
  assert.equal(purge.body.confirmed, true);
});

test("刷新或切换后失效的确认按钮不能使用旧删除修订", async () => {
  const page = fixture(({ url }) => Promise.resolve(url.includes("delete-preview")
    ? { revision: "old-revision", retain_count: 1, revocable_count: 1 } : { summary: {} }));
  await page.module.render(page.options);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  await settle();
  const confirm = page.nodes.get("#cards-batch-confirm");
  page.nodes.get("#cards-refresh").click();
  await settle();
  confirm.click();
  await settle();
  assert.equal(page.requests.filter((request) => request.method === "DELETE").length, 0);
});

test("删除预览迟到不会改写离开的页面，失效修订不能确认", async () => {
  const pending = deferred();
  const page = fixture(({ url }) => url.includes("delete-preview") ? pending.promise : Promise.resolve({ summary: {} }));
  let current = true;
  await page.module.render({ ...page.options, isCurrent: () => current });
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  current = false;
  page.workspace.innerHTML = "另一个页面";
  pending.resolve({ revision: "late-revision", retain_count: 1 });
  await settle();
  assert.equal(page.workspace.innerHTML, "另一个页面");
  assert.equal(page.nodes.get("#cards-batch-review").innerHTML, "");
  assert.equal(page.requests.filter((request) => request.method === "DELETE").length, 0);
});

test("没有修订的删除预览只显示错误，不提供确认按钮", async () => {
  const page = fixture(({ url }) => Promise.resolve(url.includes("delete-preview") ? { retain_count: 1 } : { summary: {} }));
  await page.module.render(page.options);
  page.nodes.get("#cards-inventory").querySelectorAll("[data-card-batch-review]")[0].click();
  await settle();
  assert.match(page.nodes.get("#cards-error").textContent, /没有收到批次修订/);
  assert.equal(page.nodes.get("#cards-batch-review").hidden, true);
});

test("制卡属性为本批覆盖，保留规格默认值且可使用任意商家标量键", async () => {
  const page = fixture(issueResponse);
  page.options.products[0].variants = [{ id: "default", name: "强化版", attributes: { edits: 1, tier: "plus" } }];
  page.options.products[0].revision_policy = { attribute_key: "edits", label: { "zh-CN": "修改次数" } };
  await page.module.render(page.options);
  assert.match(page.nodes.get("#cards-variant-attributes").textContent, /"edits": 1/);
  page.nodes.get("#cards-attributes").value = '{"edits":2,"purpose":"测试","priority":true}';
  page.nodes.get("#cards-count").value = "1";
  page.nodes.get("#cards-issue").fire("submit");
  await settle();
  const call = page.requests.find((row) => row.method === "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(call.body.attributes)), { edits: 2, purpose: "测试", priority: true });
  assert.equal(page.options.products[0].variants[0].attributes.edits, 1);
});

test("非法属性和非整数修改额度不能发行卡密", async () => {
  for (const attributes of ['[]', '{"edits":-1}', '{"edits":"1"}', '{"edits":true}', '{"edits":null}', '{"nested":{"a":1}}', '{"__proto__":1}']) {
    const page = fixture(issueResponse);
    page.options.products[0].revision_policy = { attribute_key: "edits" };
    await page.module.render(page.options);
    page.nodes.get("#cards-attributes").value = attributes;
    page.nodes.get("#cards-count").value = "1";
    page.nodes.get("#cards-issue").fire("submit");
    await settle();
    assert.equal(page.requests.filter((row) => row.method === "POST").length, 0);
    assert.ok(page.nodes.get("#cards-error").textContent);
  }
});

test("切换制卡规格会清空本批覆盖，展示下一个规格的卡密属性", async () => {
  const page = fixture(issueResponse);
  page.options.products[0].variants = [{ id: "default", name: "标准", attributes: { edits: 0 } }, { id: "plus", name: "强化", attributes: { edits: 1 } }];
  await page.module.render(page.options);
  page.nodes.get("#cards-attributes").value = '{"edits":7}';
  page.nodes.get("#cards-issue-variant").value = "plus";
  page.nodes.get("#cards-issue-variant").fire("change");
  await settle();
  assert.equal(page.nodes.get("#cards-attributes").value, "");
  assert.match(page.nodes.get("#cards-variant-attributes").textContent, /"edits": 1/);
});
