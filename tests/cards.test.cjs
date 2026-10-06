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

function fixture(handler, { language = "zh-CN" } = {}) {
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
