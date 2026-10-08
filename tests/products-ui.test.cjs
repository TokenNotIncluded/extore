const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page(options = {}) {
  const nodes = new Map();
  const requests = [];
  const notifications = [];
  const saved = [];
  let active = true;

  function decode(value) {
    return String(value)
      .replaceAll("&quot;", '"')
      .replaceAll("&#39;", "'")
      .replaceAll("&lt;", "<")
      .replaceAll("&gt;", ">")
      .replaceAll("&amp;", "&");
  }

  function element(id = "") {
    const listeners = new Map();
    const item = {
      id,
      value: "",
      checked: false,
      disabled: false,
      readOnly: false,
      textContent: "",
      hidden: false,
      dataset: {},
      style: {},
      attributes: {},
      isConnected: true,
      focus() {
        this.focused = true;
      },
      select() {
        this.selected = true;
      },
      addEventListener(name, handler) {
        listeners.set(name, [...(listeners.get(name) || []), handler]);
      },
      async emit(name) {
        const event = { target: item, preventDefault() {} };
        for (const handler of listeners.get(name) || []) await handler(event);
      },
      setAttribute(name, value) {
        this.attributes[name] = value;
        if (name === "disabled") this.disabled = true;
        if (name === "readonly") this.readOnly = true;
      },
      removeAttribute(name) {
        delete this.attributes[name];
        if (name === "disabled") this.disabled = false;
        if (name === "readonly") this.readOnly = false;
      },
      getAttribute(name) {
        if (name.startsWith("data-")) {
          const key = name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
          return this.dataset[key];
        }
        return this.attributes[name];
      },
      closest() {
        return this.container ||= { hidden: false };
      },
      querySelector: (selector) => find(selector),
      querySelectorAll: (selector) => findAll(selector),
    };
    let markup = "";
    let children = [];
    Object.defineProperty(item, "innerHTML", {
      get() {
        return markup;
      },
      set(value) {
        markup = String(value);
        if (id === "workspace") {
          for (const nodeId of nodes.keys()) if (nodeId !== "workspace") nodes.delete(nodeId);
        } else {
          for (const nodeId of children) nodes.delete(nodeId);
        }
        children = parse(markup);
      },
    });
    return item;
  }

  function parse(markup) {
    const parsed = [];
    for (const tag of markup.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
      const [, kind, attributes] = tag;
      const explicitId = attributes.match(/\bid="([^"]+)"/)?.[1];
      if (!explicitId && !/\bdata-[a-z-]+="/i.test(attributes)) continue;
      const id = explicitId || `anonymous-${nodes.size}`;
      const item = element(id);
      nodes.set(id, item);
      parsed.push(id);
      item.disabled = /(?:^|\s)disabled(?:\s|=|$)/i.test(attributes);
      item.readOnly = /(?:^|\s)readonly(?:\s|=|$)/i.test(attributes);
      item.checked = /(?:^|\s)checked(?:\s|=|$)/i.test(attributes);
      const value = attributes.match(/\bvalue="([^"]*)"/);
      if (value) item.value = decode(value[1]);
      for (const attribute of attributes.matchAll(/\bdata-([a-z-]+)="([^"]*)"/gi)) {
        const key = attribute[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase());
        item.dataset[key] = decode(attribute[2]);
      }
      if (kind.toLowerCase() === "textarea") {
        const body = markup.slice(tag.index + tag[0].length).split("</textarea>")[0];
        item.value = decode(body);
      }
      if (kind.toLowerCase() === "select") {
        const body = markup.slice(tag.index + tag[0].length).split("</select>")[0];
        const choices = [...body.matchAll(/<option\b([^>]*)>/gi)];
        const selected = choices.find((choice) => /\bselected\b/i.test(choice[1])) || choices[0];
        item.value = decode(selected?.[1].match(/\bvalue="([^"]*)"/)?.[1] || "");
      }
    }
    return parsed;
  }

  function find(selector) {
    if (selector.startsWith("#")) return nodes.get(selector.slice(1)) || null;
    return findAll(selector)[0] || null;
  }
  function findAll(selector) {
    const data = selector.match(/^\[data-([a-z-]+)(?:="([^"]+)")?\]$/);
    if (data) {
      const key = data[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      return [...nodes.values()].filter((item) =>
        Object.hasOwn(item.dataset, key) && (data[2] === undefined || item.dataset[key] === data[2]),
      );
    }
    return [];
  }

  const workspace = element("workspace");
  nodes.set("workspace", workspace);
  const browser = { addEventListener() {} };
  const context = vm.createContext({
    window: browser,
    document: { querySelector: find, querySelectorAll: findAll },
    structuredClone,
    JSON,
    console,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    DOMPurify: { sanitize: (value) => value },
    marked: { parse: (value) => value },
    navigator: options.navigator || {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../extore/static/products.js"), "utf8");
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../extore/static/product-export.js"), "utf8"), context);
  browser.ExtoreClipboard = options.clipboard || { async writeText() { return false; } };
  vm.runInContext(source, context);
  const ctx = {
    workspace,
    products: options.products || [],
    role: options.role || "admin",
    canConfigure: options.canConfigure ?? true,
    canEdit: options.canEdit ?? true,
    canDelete: options.canDelete ?? false,
    canPurge: options.canPurge ?? false,
    productId: options.productId || null,
    canManageCards: options.canManageCards ?? false,
    lang: "zh-CN",
    isCurrent: () => active,
    api(url, body, method) {
      return new Promise((resolve, reject) => requests.push({ url, body, method, resolve, reject }));
    },
    onSaved: (value) => saved.push(value),
    notify: (value) => notifications.push(value),
    refreshTools() {},
  };
  return {
    ctx,
    workspace,
    requests,
    notifications,
    saved,
    node: find,
    ui: browser.ExtoreProducts,
    leave: () => { active = false; },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const fieldDefinition = (key, type = "text") => ({
  key,
  label: { "zh-CN": key, en: key },
  description: { "zh-CN": "填写教程" },
  type,
  required: true,
  collapsed: true,
});
const product = (overrides = {}) => ({
  id: "product-one",
  name: "第一个商品",
  description: "商品说明",
  parameters: [fieldDefinition("email", "email")],
  outputs: [fieldDefinition("receipt", "url")],
  mode: "manual",
  delivery: "content",
  view_policy: "repeat",
  allow_retry: true,
  max_attempts: 3,
  processor_id: "",
  processor_config: {},
  ...overrides,
});
const templates = [{ id: "manual_content", name: { "zh-CN": "队列内容" } }];
const catalog = [{
  id: "personalized_text",
  name: { "zh-CN": "个性化文本" },
  description: { "zh-CN": "生成顾客专属文本" },
  delivery: "content",
  parameters: [fieldDefinition("name")],
  outputs: [fieldDefinition("content", "textarea")],
  configuration: [{ ...fieldDefinition("template", "textarea"), default: "你好，$name！", secret: true }],
}];

test("product search and visibility filters use only the current authorized snapshot", async () => {
  const p = page({ role: "staff", products: [
    product({ id: "first", name: "Research report", public: true, variants: [{ id: "word", name: "Word Plus", price: "25.123456", currency: "CNY", enabled: true }] }),
    product({ id: "second", name: "Private workshop", public: false, mode: "script", variants: [{ id: "basic", name: "Basic", price: null, enabled: true }] }),
  ] });
  p.ctx.lang = "en";
  const original = JSON.stringify(p.ctx.products);
  await p.ui.render(p.ctx);
  assert.equal(p.requests.length, 0);
  assert.equal(p.node("#products-result-count").textContent, "Showing 2 of 2 products");
  assert.match(p.workspace.innerHTML, /Reference price 25\.123456 CNY/);
  assert.match(p.workspace.innerHTML, /Product processor/);
  assert.match(p.workspace.innerHTML, /Code holders only/);
  assert.doesNotMatch(p.workspace.innerHTML, /未设置参考价|仅持卡可见|商品处理器/);
  p.node("#products-search").value = "word PLUS";
  await p.node("#products-search").emit("input");
  assert.equal(p.node('[data-product-row="first"]').hidden, false);
  assert.equal(p.node('[data-product-row="second"]').hidden, true);
  assert.equal(p.node("#products-result-count").textContent, "Showing 1 of 2 products");
  p.node("#products-status").value = "private";
  await p.node("#products-status").emit("change");
  assert.equal(p.node("#products-no-match").hidden, false);
  assert.equal(p.node("#products-result-count").textContent, "Showing 0 of 2 products");
  await p.node("#products-reset").emit("click");
  assert.equal(p.node("#products-search").value, "");
  assert.equal(p.node("#products-status").value, "all");
  assert.equal(p.node("#products-no-match").hidden, true);
  assert.equal(p.node('[data-product-row="second"]').hidden, false);
  assert.equal(p.node("#products-search").focused, true);
  assert.equal(p.requests.length, 0);
  assert.equal(JSON.stringify(p.ctx.products), original);
});

test("local filters cannot change recycle-bin cleanup scope or hide its confirmation snapshot", async () => {
  const p = page({ products: [
    product({ id: "first", name: "Find me", deleted: true, shop_id: "shop-a" }),
    product({ id: "second", name: "Hidden by search", deleted: true, shop_id: "shop-a" }),
  ] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  p.node("#products-search").value = "Find me";
  await p.node("#products-search").emit("input");
  assert.equal(p.node('[data-product-row="second"]').hidden, true);
  assert.equal(p.node("#products-status"), null);
  assert.equal(p.node("#empty-product-trash").disabled, false);
  assert.match(p.workspace.innerHTML, /搜索只影响显示/);
  await p.node("#empty-product-trash").emit("click");
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /Find me/);
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /Hidden by search/);
  await p.node("#confirm-product-purge").emit("click");
  assert.equal(p.requests[0].url, "/admin/products/empty-trash?shop_id=shop-a");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body)), { confirmed: true, product_ids: ["first", "second"] });
});

test("local search never narrows quick-copy template sources and quick creation starts folded", async () => {
  const p = page({ products: [product({ id: "first", name: "Find me" }), product({ id: "second", name: "Other product" })] });
  const rendering = p.ui.render(p.ctx, { productId: "first", productName: "Created product", url: "https://example.test/staff#full-link" });
  assert.equal(p.requests[0].url, "/admin/product-templates");
  assert.match(p.workspace.innerHTML, /<details class="products-quick-create">/);
  const quickMarkup = p.workspace.innerHTML.split('<details class="products-quick-create">')[1].split("</details>")[0];
  assert.doesNotMatch(quickMarkup, /quick-created|quick-management-link/);
  assert.equal(p.node("#quick-management-link").value, "https://example.test/staff#full-link");
  p.node("#products-search").value = "Find me";
  await p.node("#products-search").emit("input");
  p.requests[0].resolve(templates);
  await rendering;
  assert.equal(p.node('[data-product-row="second"]').hidden, true);
  assert.match(p.node("#quick-template").innerHTML, /existing_product:first/);
  assert.match(p.node("#quick-template").innerHTML, /existing_product:second/);
  assert.equal(p.node("#products-search").value, "Find me");
});

test("product filtering ignores credential-like fields and expired page controls", async () => {
  const p = page({ role: "staff", products: [product({ name: "Visible name", processor_config: { secret: "NEVER_SEARCH_PRIVATE_CONFIGURATION" } })] });
  await p.ui.render(p.ctx);
  const oldSearch = p.node("#products-search");
  oldSearch.value = "NEVER_SEARCH_PRIVATE_CONFIGURATION";
  await oldSearch.emit("input");
  assert.equal(p.node('[data-product-row="product-one"]').hidden, true);
  assert.doesNotMatch(p.workspace.innerHTML, /NEVER_SEARCH_PRIVATE_CONFIGURATION/);
  p.leave();
  p.workspace.innerHTML = '<p id="products-result-count">A different page</p>';
  const currentCounter = p.node("#products-result-count");
  currentCounter.textContent = "Keep this page";
  oldSearch.value = "Visible name";
  await oldSearch.emit("input");
  assert.equal(currentCounter.textContent, "Keep this page");
  assert.equal(p.requests.length, 0);
});

test("product rows escape long content and keep excess variant details available without expanded cards", async () => {
  const name = '<img src="x" onerror="PRIVATE_EXECUTION()">' + "LongName".repeat(60);
  const p = page({ role: "staff", products: [product({ name, logo: 'https://example.test/logo.png" onload="PRIVATE_EXECUTION()', variants: Array.from({ length: 5 }, (_, index) => ({ id: "v" + index, name: "Variant-" + index + "-" + "long".repeat(40), price: index === 4 ? "99.000001" : null, currency: "CNY", enabled: index !== 4 })) })] });
  await p.ui.render(p.ctx);
  assert.match(p.workspace.innerHTML, /&lt;img src=&quot;x&quot;/);
  assert.doesNotMatch(p.workspace.innerHTML, /onload="PRIVATE_EXECUTION|onerror="PRIVATE_EXECUTION/);
  assert.match(p.workspace.innerHTML, /查看其余 2 个规格/);
  assert.match(p.workspace.innerHTML, /参考价 99\.000001 CNY/);
  assert.match(p.workspace.innerHTML, /已停用/);
  assert.match(p.workspace.innerHTML, /products-workspace/);
  assert.ok(p.node('[data-edit="product-one"]'));
});

async function renderOwner(p) {
  const rendering = p.ui.render(p.ctx);
  assert.equal(p.requests[0].url, "/admin/product-templates");
  p.requests[0].resolve(templates);
  await rendering;
}

async function editProduct(p, value = product(), processors = catalog) {
  const editing = p.ui.edit(p.ctx, value);
  assert.equal(p.requests[0].url, p.ctx.role === "staff" ? "/manage/processors" : "/admin/processors");
  p.requests[0].resolve(processors);
  await editing;
}

test("trash purge is independently delegated and an empty recycle bin cannot be cleared", async () => {
  const p = page({ role: "staff", canEdit: false, canDelete: true, products: [product({ deleted: true })] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  assert.equal(p.node("#empty-product-trash"), null);
  assert.equal(p.node('[data-purge="product-one"]'), null);
  const empty = page({ role: "staff", canEdit: false, canPurge: true });
  empty.ctx.productView = "deleted";
  await empty.ui.render(empty.ctx);
  assert.equal(empty.node("#empty-product-trash").disabled, true);
  await empty.node("#empty-product-trash").emit("click");
  assert.equal(empty.requests.length, 0);
});

test("trash clear confirms an immutable visible snapshot and cancellation performs no write", async () => {
  const p = page({ products: [product({ deleted: true, shop_id: "shop-a" }), product({ id: "second", name: "Second", deleted: true, shop_id: "shop-a" })] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  await p.node("#empty-product-trash").emit("click");
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /彻底删除 2 个商品/);
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /商品不能恢复.*旧卡密仍可兑换/);
  assert.equal(p.requests.length, 0);
  const oldConfirm = p.node("#confirm-product-purge");
  await p.node("#cancel-product-purge").emit("click");
  await oldConfirm.emit("click");
  assert.equal(p.requests.length, 0);
  await p.node("#empty-product-trash").emit("click");
  p.ctx.products.push(product({ id: "late", name: "LATE ARRIVAL", deleted: true, shop_id: "shop-a" }));
  await p.node("#confirm-product-purge").emit("click");
  assert.equal(p.requests[0].url, "/admin/products/empty-trash?shop_id=shop-a");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body)), { confirmed: true, product_ids: ["product-one", "second"] });
  p.requests[0].resolve({ ok: true, purged_product_ids: ["second", "product-one"], purged_count: 2, preserved_fulfillment: true });
  await flush();
  p.requests[1].resolve([product({ id: "late", name: "LATE ARRIVAL", deleted: true, shop_id: "shop-a" })]);
  await flush();
  assert.equal(p.saved[0].find((product) => product.id === "product-one").purged, true);
  assert.ok(p.node('[data-purge="late"]'));
  assert.equal(p.node('[data-purge="product-one"]'), null);
});

test("root trash clearing requires one selected shop and never mixes its product IDs", async () => {
  const p = page({ products: [product({ deleted: true, shop_id: "shop-a", shop_name: "Shop A" }), product({ id: "second", name: "Shop B product", deleted: true, shop_id: "shop-b", shop_name: "Shop B" })] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  assert.equal(p.node("#empty-product-trash").disabled, true);
  p.node("#trash-shop").value = "shop-a";
  await p.node("#trash-shop").emit("change");
  assert.equal(p.node("#empty-product-trash").disabled, false);
  await p.node("#empty-product-trash").emit("click");
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /彻底删除 1 个商品/);
  assert.doesNotMatch(p.node("#product-lifecycle-confirmation").innerHTML, /Shop B product/);
  await p.node("#confirm-product-purge").emit("click");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body.product_ids)), ["product-one"]);
  assert.match(p.requests[0].url, /shop_id=shop-a/);
  p.requests[0].reject(new Error("Network unavailable"));
  await flush();
});

test("permanent purge failure preserves trash and re-enables explicit confirmation", async () => {
  const p = page({ role: "staff", canEdit: false, canPurge: true, productId: "product-one", products: [product({ deleted: true })] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  await p.node('[data-purge="product-one"]').emit("click");
  await p.node("#confirm-product-purge").emit("click");
  assert.equal(p.requests[0].url, "/manage/product/purge?product_id=product-one");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body)), { confirmed: true });
  assert.equal(p.node("#confirm-product-purge").disabled, true);
  p.requests[0].reject(new Error("Permission revoked"));
  await flush();
  assert.equal(p.node("#confirm-product-purge").disabled, false);
  assert.equal(p.node("#cancel-product-purge").disabled, false);
  assert.match(p.node("#error").textContent, /Permission revoked/);
  assert.equal(p.saved.length, 0);
  assert.ok(p.node('[data-purge="product-one"]'));
});

test("late permanent purge results and malformed success DTOs cannot replace another view", async () => {
  const p = page({ role: "staff", canEdit: false, canPurge: true, products: [product({ deleted: true })] });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  await p.node("#empty-product-trash").emit("click");
  await p.node("#confirm-product-purge").emit("click");
  p.requests[0].resolve({ ok: true, purged_product_ids: ["other"], purged_count: 1, preserved_fulfillment: true });
  await flush();
  assert.match(p.node("#error").textContent, /未能确认清理结果/);
  assert.equal(p.saved.length, 0);
  await p.node("#confirm-product-purge").emit("click");
  p.leave();
  p.workspace.innerHTML = "another view";
  p.requests[1].resolve({ ok: true, purged_product_ids: ["product-one"], purged_count: 1, preserved_fulfillment: true });
  await flush();
  assert.equal(p.workspace.innerHTML, "another view");
  assert.equal(p.requests.length, 2);
  assert.equal(p.saved.length, 0);
});

test("oversized trash snapshots never silently truncate or send a partial purge", async () => {
  const p = page({ role: "staff", canEdit: false, canPurge: true, products: Array.from({ length: 501 }, (_, index) => product({ id: "item-" + index, deleted: true, shop_id: "shop-a" })) });
  p.ctx.productView = "deleted";
  await p.ui.render(p.ctx);
  assert.equal(p.node("#empty-product-trash").disabled, true);
  await p.node("#empty-product-trash").emit("click");
  assert.equal(p.requests.length, 0);
  assert.match(p.workspace.innerHTML, /500/);
});

test("purged products disappear even from all-products DTOs and cannot be restored or configured", async () => {
  const p = page({ role: "staff", canEdit: true, canDelete: true, canPurge: true, products: [product({ purged: true, purged_at: 123 })] });
  p.ctx.productView = "all";
  await p.ui.render(p.ctx);
  for (const name of ["edit", "export", "delete", "restore", "purge"]) assert.equal(p.node(`[data-${name}="product-one"]`), null);
  await p.ui.edit(p.ctx, product({ purged: true, purged_at: 123 }));
  assert.equal(p.requests.length, 0);
});

test("deleting a product requires the inline confirmation and cancel sends no mutation", async () => {
  const p = page({ role: "staff", canDelete: true, products: [product()] });
  await p.ui.render(p.ctx);
  await p.node('[data-delete="product-one"]').emit("click");
  assert.match(p.node("#product-lifecycle-confirmation").innerHTML, /已有卡密、领取链接与未完成任务仍然有效/);
  assert.equal(p.requests.length, 0);
  const previousConfirm = p.node("#confirm-product-delete");
  await p.node("#cancel-product-delete").emit("click");
  assert.equal(p.node("#product-lifecycle-confirmation").innerHTML, "");
  await previousConfirm.emit("click");
  assert.equal(p.requests.length, 0);
});

test("only the explicit delete capability allows a staff manager to delete or restore", async () => {
  const editor = page({ role: "staff", canEdit: true, canDelete: false, products: [product()] });
  await editor.ui.render(editor.ctx);
  assert.equal(editor.node('[data-delete="product-one"]'), null);
  const manager = page({ role: "staff", canEdit: false, canDelete: true, products: [product()] });
  await manager.ui.render(manager.ctx);
  assert.ok(manager.node('[data-delete="product-one"]'));
  assert.equal(manager.node('[data-edit="product-one"]'), null);
  assert.equal(manager.node('[data-export="product-one"]'), null);
  await manager.ui.edit(manager.ctx, product());
  assert.equal(manager.requests.length, 0);
  assert.match(manager.notifications[0], /编辑权限/);
});

test("successful deletion reloads the active list and never removes existing cards or tasks", async () => {
  const p = page({ role: "staff", canDelete: true, products: [product()] });
  await p.ui.render(p.ctx);
  await p.node('[data-delete="product-one"]').emit("click");
  await p.node("#confirm-product-delete").emit("click");
  assert.equal(p.requests[0].url, "/manage/product?product_id=product-one");
  assert.equal(p.requests[0].method, "DELETE");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[0].body)), { confirmed: true });
  p.requests[0].resolve({ ok: true, product_id: "product-one", deleted: true, deleted_at: 123 });
  await flush();
  assert.equal(p.requests[1].url, "/manage/products?view=active");
  p.requests[1].resolve([]);
  await flush();
  assert.equal(p.node('[data-delete="product-one"]'), null);
  assert.equal(p.saved[0][0].deleted, true);
  assert.deepEqual(p.saved.at(-1), []);
  assert.equal(p.requests.filter((request) => /cards|jobs/.test(request.url)).length, 0);
});

test("deletion errors retain the confirmation and re-enable the controls for another try", async () => {
  const p = page({ role: "staff", canDelete: true, products: [product()] });
  await p.ui.render(p.ctx);
  await p.node('[data-delete="product-one"]').emit("click");
  await p.node("#confirm-product-delete").emit("click");
  assert.equal(p.node("#confirm-product-delete").disabled, true);
  assert.equal(p.node("#cancel-product-delete").disabled, true);
  p.requests[0].reject(new Error("网络连接失败，请重试"));
  await flush();
  assert.equal(p.node("#confirm-product-delete").disabled, false);
  assert.equal(p.node("#cancel-product-delete").disabled, false);
  assert.equal(p.node("#error").textContent, "网络连接失败，请重试");
  assert.ok(p.node('[data-delete="product-one"]'));
});

test("a late deletion response cannot render another page or update its product cache", async () => {
  const p = page({ role: "staff", canDelete: true, products: [product()] });
  await p.ui.render(p.ctx);
  await p.node('[data-delete="product-one"]').emit("click");
  await p.node("#confirm-product-delete").emit("click");
  p.leave();
  p.workspace.innerHTML = "another page";
  p.requests[0].resolve({ ok: true, product_id: "product-one", deleted: true, deleted_at: 123 });
  await flush();
  assert.equal(p.workspace.innerHTML, "another page");
  assert.equal(p.requests.length, 1);
  assert.equal(p.saved.length, 0);
});

test("the recycle bin restores products but exposes no configuration, export or creation controls", async () => {
  const p = page({ products: [product({ deleted: true, deleted_at: 123 })] });
  p.ctx.productView = "deleted";
  p.ctx.lang = "en";
  await p.ui.render(p.ctx);
  assert.equal(p.requests.length, 0);
  assert.equal(p.node("#new-product"), null);
  assert.equal(p.node("#quick-product"), null);
  assert.equal(p.node('[data-edit="product-one"]'), null);
  assert.equal(p.node('[data-export="product-one"]'), null);
  assert.match(p.workspace.innerHTML, /existing codes and tasks remain valid/);
  const restoring = p.node('[data-restore="product-one"]').emit("click");
  assert.equal(p.requests[0].url, "/admin/products/product-one/restore");
  assert.equal(p.requests[0].method, "POST");
  p.requests[0].resolve({ ok: true, product_id: "product-one", deleted: false, deleted_at: null });
  await flush();
  p.requests[1].resolve([]);
  await restoring;
  assert.equal(p.node('[data-restore="product-one"]'), null);
  assert.match(p.workspace.innerHTML, /recycle bin is empty/);
});

test("product view selection fetches only its chosen lifecycle list and ignores a late result", async () => {
  const p = page({ role: "staff", canDelete: true, products: [product()] });
  await p.ui.render(p.ctx);
  p.node("#products-view").value = "deleted";
  await p.node("#products-view").emit("change");
  assert.equal(p.requests[0].url, "/manage/products?view=deleted");
  p.leave();
  p.requests[0].resolve([product({ deleted: true, deleted_at: 123 })]);
  await flush();
  assert.equal(p.saved.length, 0);
});

test("all-products view never offers deleted products as quick-copy templates", async () => {
  const p = page({ products: [product(), product({ id: "deleted-product", name: "DELETED", deleted: true })] });
  p.ctx.productView = "all";
  await renderOwner(p);
  assert.match(p.node("#quick-template").innerHTML, /existing_product:product-one/);
  assert.doesNotMatch(p.node("#quick-template").innerHTML, /existing_product:deleted-product/);
  assert.equal(p.node('[data-edit="deleted-product"]'), null);
});

test("only the owner can create or copy product drafts", async () => {
  const staff = page({ role: "staff", products: [product()] });
  await staff.ui.render(staff.ctx);
  assert.equal(staff.requests.length, 0);
  assert.equal(staff.node("#new-product"), null);
  assert.equal(staff.node("#quick-product"), null);
  assert.match(staff.workspace.innerHTML, /队列/);
  assert.doesNotMatch(staff.workspace.innerHTML, /人工/);

  const owner = page({ products: [product()] });
  await renderOwner(owner);
  assert.equal(owner.node("#quick-product").disabled, false);
  assert.match(owner.node("#quick-template").innerHTML, /manual_content/);
  assert.match(owner.node("#quick-template").innerHTML, /existing_product:product-one/);
});

test("late templates and catalog reads preserve the page after navigation", async () => {
  for (const operation of ["render", "edit"]) {
    const p = page();
    const pending = operation === "render" ? p.ui.render(p.ctx) : p.ui.edit(p.ctx, product());
    p.leave();
    p.workspace.innerHTML = "A different management page";
    p.requests[0].resolve(operation === "render" ? templates : catalog);
    await pending;
    assert.equal(p.workspace.innerHTML, "A different management page");
  }
});

test("a new editor supersedes an earlier template request in the same workspace", async () => {
  const p = page();
  const listing = p.ui.render(p.ctx);
  const editing = p.ui.edit(p.ctx, product());
  const editor = p.workspace.innerHTML;
  p.requests[0].resolve(templates);
  await listing;
  assert.equal(p.workspace.innerHTML, editor);
  p.requests[1].resolve(catalog);
  await editing;
  assert.equal(p.node("#p-name").value, "第一个商品");
});

test("quick creation submits the selected template and shows its scoped link", async () => {
  const p = page();
  await renderOwner(p);
  p.node("#quick-template").value = "manual_content";
  await p.node("#quick-product").emit("click");
  assert.equal(p.requests[1].url, "/admin/products/quick");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[1].body)), { template_id: "manual_content" });
  const created = product({ id: "private-draft", name: "随机草稿", public: false });
  p.requests[1].resolve({ product: created, management_link: { url: "https://example.test/staff#limited-token" } });
  await flush();
  assert.equal(p.saved.length, 1);
  assert.equal(p.saved[0][0], created);
  assert.equal(p.node("#quick-management-link").value, "https://example.test/staff#limited-token");
  assert.equal(p.node("#quick-management-link").readOnly, true);
  assert.match(p.workspace.innerHTML, /只允许编辑当前商品与发货配置/);
  p.requests[2].resolve(templates);
  await flush();
  assert.deepEqual(p.notifications, ["私有商品草稿已创建"]);
});

test("copying an existing product keeps the source id and a late write only updates saved data", async () => {
  const original = product();
  const p = page({ products: [original] });
  await renderOwner(p);
  p.node("#quick-template").value = "existing_product:product-one";
  await p.node("#quick-product").emit("click");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[1].body)), {
    template_id: "existing_product",
    from_product_id: "product-one",
  });
  p.leave();
  p.workspace.innerHTML = "Different page";
  const copy = product({ id: "copy" });
  p.requests[1].resolve({ product: copy, management_link: { url: "https://example.test/staff#secret-copy" } });
  await flush();
  assert.equal(p.workspace.innerHTML, "Different page");
  assert.deepEqual(JSON.parse(JSON.stringify(p.saved[0])), [original, copy]);
  assert.equal(p.requests.length, 2);
  assert.equal(p.notifications.length, 0);
});

test("queue input and output definitions are submitted from the edited controls", async () => {
  const p = page({ products: [product()] });
  await editProduct(p);
  p.node("#f-key-0").value = "account";
  p.node("#f-label-0").value = '{"zh-CN":"账号","en":"Account"}';
  p.node("#o-key-0").value = "resource";
  p.node("#o-label-0").value = '{"zh-CN":"资源链接"}';
  p.node("#product-form").emit("submit");
  assert.equal(p.requests[1].url, "/admin/products/product-one");
  assert.equal(p.requests[1].method, "PUT");
  const body = JSON.parse(JSON.stringify(p.requests[1].body));
  assert.equal(body.mode, "manual");
  assert.equal(body.parameters[0].key, "account");
  assert.deepEqual(body.parameters[0].label, { "zh-CN": "账号", en: "Account" });
  assert.equal(body.outputs[0].key, "resource");
  assert.deepEqual(body.outputs[0].label, { "zh-CN": "资源链接" });
  assert.equal(body.script, "");
});

test("invalid translated schema JSON blocks saving and explains the field", async () => {
  const p = page();
  await editProduct(p);
  p.node("#o-label-0").value = '{"zh-CN":23}';
  await p.node("#product-form").emit("submit");
  await flush();
  assert.equal(p.requests.length, 1);
  assert.match(p.node("#error").textContent, /显示名称需要填写/);
  assert.equal(p.node("#save-product").disabled, false);
});

test("loading the processor catalog preserves already typed queue field definitions", async () => {
  const p = page();
  const editing = p.ui.edit(p.ctx, product());
  p.node("#f-key-0").value = "typed-account";
  p.node("#o-label-0").value = '{"zh-CN":"已输入的结果名称"}';
  p.requests[0].resolve(catalog);
  await editing;
  assert.equal(p.node("#f-key-0").value, "typed-account");
  assert.equal(p.node("#o-label-0").value, '{"zh-CN":"已输入的结果名称"}');
});

test("invalid schema JSON prevents changing handling or delivery and preserves the draft", async () => {
  for (const [selector, next, previous] of [
    ["#p-mode", "script", "manual"],
    ["#p-delivery", "service", "content"],
  ]) {
    const p = page();
    await editProduct(p);
    p.node("#f-label-0").value = "unfinished JSON";
    p.node(selector).value = next;
    await p.node(selector).emit("change");
    await flush();
    assert.equal(p.node(selector).value, previous);
    assert.equal(p.node("#f-label-0").value, "unfinished JSON");
    assert.match(p.node("#error").textContent, /显示名称需要填写/);
    assert.equal(p.requests.length, 1);
  }
});

test("switching between queue and a processor retains queue drafts without exposing shop secrets", async () => {
  const p = page();
  await editProduct(p);
  p.node("#f-key-0").value = "draft-account";
  p.node("#o-key-0").value = "draft-delivery";
  p.node("#p-mode").value = "script";
  await p.node("#p-mode").emit("change");
  p.node("#p-processor").value = "personalized_text";
  await p.node("#p-processor").emit("change");
  assert.equal(p.node("#pc-value-0"), null);
  assert.equal(p.requests[1].url, "/admin/processor-profiles");
  assert.equal(p.requests[2].url, "/admin/processor-profiles/bindings/product-one");
  p.requests[1].resolve([]);
  p.requests[2].resolve({ product_id: "product-one", profile: null });
  await flush();
  p.node("#p-mode").value = "manual";
  await p.node("#p-mode").emit("change");
  assert.equal(p.node("#f-key-0").value, "draft-account");
  assert.equal(p.node("#o-key-0").value, "draft-delivery");
  p.node("#p-mode").value = "script";
  await p.node("#p-mode").emit("change");
  assert.equal(p.node("#pc-value-0"), null);
  assert.match(p.node("#parameters").innerHTML, /name · text/);
  assert.match(p.node("#outputs").innerHTML, /content · textarea/);
});

test("instant text mode removes queue work and submits one frozen text result", async () => {
  const p = page();
  await editProduct(p);
  p.node("#p-mode").value = "stock";
  await p.node("#p-mode").emit("change");
  assert.equal(p.node("#p-delivery").value, "content");
  assert.equal(p.node("#p-delivery").disabled, true);
  assert.equal(p.node("#product-progress-section").hidden, true);
  assert.equal(p.node("#stock-help").hidden, false);
  assert.equal(p.node("#add-param").hidden, true);
  assert.match(p.node("#outputs").innerHTML, /content · textarea/);
  assert.equal(p.node("#f-key-0"), null);
  await p.node("#product-form").emit("submit");
  const saving = p.requests.at(-1);
  assert.equal(saving.body.mode, "stock");
  assert.equal(saving.body.parameters.length, 0);
  assert.equal(saving.body.outputs.length, 1);
  assert.equal(saving.body.outputs[0].key, "content");
  assert.equal(saving.body.progress_steps.length, 0);
  saving.resolve({ id: "product-one", ...saving.body });
  await flush();
});

test("clipboard denial still selects the complete scoped management link", async () => {
  const p = page({ navigator: { clipboard: { async writeText() { throw new Error("permission denied"); } } } });
  const rendering = p.ui.render(p.ctx, {
    productId: "one",
    productName: "商品",
    url: "https://example.test/staff#complete-secret-token",
  });
  p.requests[0].resolve(templates);
  await rendering;
  await p.node("#copy-quick-link").emit("click");
  await flush();
  const input = p.node("#quick-management-link");
  assert.equal(input.value, "https://example.test/staff#complete-secret-token");
  assert.equal(input.focused, true);
  assert.equal(input.selected, true);
  assert.deepEqual(p.notifications, ["请复制已选中的配置链接"]);
});

test("processor schemas stay code-defined and product writes never echo masked credentials", async () => {
  const p = page();
  await editProduct(p, product({ shop_id: "shop-one", mode: "script", processor_id: "personalized_text", processor_config: { template: "credential-never-rendered" } }));
  assert.equal(p.node("#p-script"), null);
  assert.doesNotMatch(p.workspace.innerHTML, /脚本名称|服务器安装|credential-never-rendered/);
  assert.equal(p.node("#p-delivery").disabled, true);
  assert.match(p.node("#parameters").innerHTML, /data-schema-source="processor"/);
  assert.match(p.node("#outputs").innerHTML, /data-schema-source="processor"/);
  assert.equal(p.node("#f-key-0"), null);
  assert.equal(p.node("#o-key-0"), null);
  assert.equal(p.node("#pc-value-0"), null);
  p.requests[1].resolve([{ id: "profile-one", shop_id: "shop-one", processor_id: "personalized_text", name: "付款账户 <one>", revision: 2 }, { id: "foreign", shop_id: "shop-two", processor_id: "personalized_text", name: "Foreign account", revision: 1 }]);
  p.requests[2].resolve({ product_id: "product-one", profile: null });
  await flush();
  assert.match(p.node("#processor-configuration").innerHTML, /付款账户 &lt;one&gt;/);
  assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /Foreign account/);
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[3].body));
  assert.equal(body.processor_id, "personalized_text");
  assert.equal(Object.hasOwn(body, "processor_config"), false);
  assert.deepEqual(body.parameters, catalog[0].parameters);
  assert.deepEqual(body.outputs, catalog[0].outputs);
  assert.equal(body.script, "");
});

const readableCatalog = [{
  ...catalog[0],
  configuration: [
    { ...fieldDefinition("template", "textarea"), secret: false, default: "Default template" },
    { ...fieldDefinition("message"), secret: false },
    { ...fieldDefinition("api_key"), secret: true },
    fieldDefinition("username"),
  ],
}];

const boundProfile = (overrides = {}) => ({
  id: "profile-one",
  shop_id: "shop-one",
  processor_id: "personalized_text",
  name: "文档交付账户",
  revision: 4,
  bound_revision: 2,
  configuration: { template: "Bound template\n$name", message: "Delivery message" },
  configured_fields: ["template", "message", "api_key"],
  ...overrides,
});

async function showBoundProfile(p, profile, listedProfile = boundProfile({ configuration: { template: "Latest template" } })) {
  await editProduct(p, product({ shop_id: "shop-one", mode: "script", processor_id: "personalized_text" }), readableCatalog);
  p.requests[1].resolve([listedProfile]);
  p.requests[2].resolve({ product_id: "product-one", profile });
  await flush();
}

test("bound templates are readable, escaped and pinned without echoing credentials or writing shared configuration", async () => {
  const p = page();
  const text = 'Bound template\n$name\n</textarea><script>unsafe()</script>&"';
  await showBoundProfile(p, boundProfile({ configuration: {
    template: text,
    message: "Delivery message",
    api_key: "CREDENTIAL_NEVER_RENDERED",
    username: "UNDECLARED_SECRET_NEVER_RENDERED",
    unknown: "UNKNOWN_NEVER_RENDERED",
  } }));
  const markup = p.node("#processor-configuration").innerHTML;
  assert.equal(p.node("#profile-preview-0").value, text);
  assert.equal(p.node("#profile-preview-0").readOnly, true);
  assert.equal(p.node("#profile-preview-1").value, "Delivery message");
  assert.equal(p.node("#profile-preview-1").readOnly, true);
  assert.equal(p.node("#profile-preview-2"), null);
  assert.match(markup, /版本 2/);
  assert.match(markup, /&lt;\/textarea&gt;&lt;script&gt;/);
  assert.match(markup, /保存后回到此商品选择新版本；已发行卡密继续使用原版本/);
  assert.doesNotMatch(markup, /<script>|Latest template|CREDENTIAL_NEVER_RENDERED|UNDECLARED_SECRET_NEVER_RENDERED|UNKNOWN_NEVER_RENDERED/);
  assert.match(markup, /id="profile-preview-1"[^>]*type="text"/);
  p.node("#profile-preview-0").value = "Edited through DOM";
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[3].body));
  assert.equal(Object.hasOwn(body, "processor_config"), false);
  assert.doesNotMatch(JSON.stringify(body), /Edited through DOM|CREDENTIAL_NEVER_RENDERED/);
});

test("explicit empty bound text remains empty and missing values never invent profile defaults", async () => {
  const p = page();
  await showBoundProfile(p, boundProfile({ configuration: { template: "", message: 10 } }));
  assert.equal(p.node("#profile-preview-0").value, "");
  assert.equal(p.node("#profile-preview-1"), null);
  assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /Default template/);
});

test("bound public select choices preview the localized label without exposing secret choices or defaults", async () => {
  const processors = [{ ...readableCatalog[0], configuration: [
    { ...fieldDefinition("format", "select"), secret: false, default: "default-not-bound", options: [{ value: "csv", label: { "zh-CN": "CSV 表格 <img onerror=bad>", en: "CSV table <img onerror=bad>" } }] },
    { ...fieldDefinition("token", "select"), secret: true, options: [{ value: "SECRET_CHOICE", label: "Hidden" }] },
  ] }];
  for (const lang of ["zh-CN", "en"]) {
    const p = page(); p.ctx.lang = lang;
    await editProduct(p, product({ shop_id: "shop-one", mode: "script", processor_id: "personalized_text" }), processors);
    p.requests[1].resolve([boundProfile()]);
    p.requests[2].resolve({ product_id: "product-one", profile: boundProfile({ configuration: { format: "csv", token: "SECRET_CHOICE" } }) }); await flush();
    assert.equal(p.node("#profile-preview-0").value, lang === "en" ? "CSV table <img onerror=bad>" : "CSV 表格 <img onerror=bad>");
    assert.equal(p.node("#profile-preview-0").readOnly, true);
    assert.equal(p.node("#profile-preview-1"), null);
    assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /<img|SECRET_CHOICE|default-not-bound/);
  }
});

test("duplicate configuration names hide conflicting secrets regardless of declaration order", async () => {
  const p = page();
  const processors = [{ ...readableCatalog[0], configuration: [
    { ...fieldDefinition("template", "textarea"), secret: false },
    { ...fieldDefinition("template"), secret: true },
    { ...fieldDefinition("template", "textarea"), secret: false },
    { ...fieldDefinition("message"), secret: false },
  ] }];
  await editProduct(p, product({ shop_id: "shop-one", mode: "script", processor_id: "personalized_text" }), processors);
  p.requests[1].resolve([boundProfile()]);
  p.requests[2].resolve({ product_id: "product-one", profile: boundProfile({ configuration: { template: "CONFLICTING_SECRET_MUST_STAY_HIDDEN", message: "Visible message" } }) });
  await flush();
  assert.equal(p.node("#profile-preview-0").value, "Visible message");
  assert.equal(p.node("#profile-preview-1"), null);
  assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /CONFLICTING_SECRET_MUST_STAY_HIDDEN/);
});

test("a bound preview requires the product's shop and processor, including the current shop fallback", async () => {
  for (const profile of [
    boundProfile({ shop_id: "shop-two", configuration: { template: "FOREIGN_SHOP_TEXT" } }),
    boundProfile({ processor_id: "other_processor", configuration: { template: "FOREIGN_PROCESSOR_TEXT" } }),
  ]) {
    const p = page();
    await showBoundProfile(p, profile);
    assert.equal(p.node("#profile-preview-0"), null);
    assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /FOREIGN_SHOP_TEXT|FOREIGN_PROCESSOR_TEXT/);
  }
  const p = page();
  p.ctx.shopId = "shop-one";
  await editProduct(p, product({ mode: "script", processor_id: "personalized_text" }), readableCatalog);
  p.requests[1].resolve([boundProfile()]);
  p.requests[2].resolve({ product_id: "product-one", profile: boundProfile() });
  await flush();
  assert.equal(p.node("#profile-preview-0").value, "Bound template\n$name");
});

test("rebind previews the returned version and unbind removes text without changing product configuration", async () => {
  const p = page();
  await showBoundProfile(p, boundProfile());
  await p.node("#bind-profile").emit("click");
  assert.equal(p.requests[3].method, "PUT");
  assert.deepEqual(JSON.parse(JSON.stringify(p.requests[3].body)), { profile_id: "profile-one" });
  p.requests[3].resolve({ product_id: "product-one", profile: boundProfile({ bound_revision: 4, configuration: { template: "New bound version" } }) });
  await flush();
  assert.equal(p.node("#profile-preview-0").value, "New bound version");
  assert.match(p.node("#processor-configuration").innerHTML, /版本 4/);
  assert.deepEqual(p.notifications, ["处理器配置已绑定，仅用于之后发行的卡密"]);
  await p.node("#unbind-profile").emit("click");
  assert.equal(p.requests[4].method, "DELETE");
  assert.equal(p.requests[4].body, null);
  p.requests[4].resolve({ ok: true });
  await flush();
  assert.equal(p.node("#profile-preview-0"), null);
  assert.match(p.node("#processor-configuration").innerHTML, /尚未绑定处理器配置/);
  assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /New bound version/);
});

test("workflow preview shows pinned plain variables, secret names and only bounded runtime fields", async () => {
  const p = page();
  const workflow = {
    variables: { OUTPUT_LOCALE: "zh-CN", INSTRUCTIONS: "Bound instructions\n</textarea><script>ignored()</script>" },
    configured_secret_names: ["API_TOKEN", "PAYMENT_ACCOUNT"],
    runtime: { timeout_seconds: 90, memory_mb: 256, cpu_seconds: 60, max_output_bytes: 1000000, command: "hidden-command" },
    secrets: { API_TOKEN: "private-workflow-token" },
  };
  await showBoundProfile(p, boundProfile({ workflow }), boundProfile({ workflow: { ...workflow, variables: { OUTPUT_LOCALE: "Latest locale" } } }));
  assert.equal(p.node("#workflow-variable-0").value, "zh-CN");
  assert.equal(p.node("#workflow-variable-0").readOnly, true);
  assert.equal(p.node("#workflow-variable-1").value, workflow.variables.INSTRUCTIONS);
  assert.equal(p.node("#workflow-variable-1").readOnly, true);
  const markup = p.node("#processor-configuration").innerHTML;
  assert.match(markup, /绑定版本 2/);
  assert.match(markup, /API_TOKEN · 已设置/);
  assert.match(markup, /PAYMENT_ACCOUNT · 已设置/);
  assert.match(markup, /最长运行时间（秒）<\/dt><dd>90/);
  assert.match(markup, /内存上限（MB）<\/dt><dd>256/);
  assert.match(markup, /输出上限（字节）<\/dt><dd>1000000/);
  assert.match(markup, /变量单独传给处理器，不会自动插入交付文本/);
  assert.match(markup, /「处理器配置」中编辑模板与变量/);
  assert.doesNotMatch(markup, /private-workflow-token|Latest locale|hidden-command|<script>|处理器账户/);
  p.node("#workflow-variable-0").value = "DOM change must not save shared config";
  p.node("#product-form").emit("submit");
  assert.equal(Object.hasOwn(p.requests[3].body, "workflow"), false);
  assert.doesNotMatch(JSON.stringify(p.requests[3].body), /DOM change|private-workflow-token/);
});

test("foreign or malformed workflow DTO values never create readable variables or leaked secrets", async () => {
  for (const profile of [
    boundProfile({ shop_id: "shop-two", workflow: { variables: { SECRET: "foreign-plain" }, configured_secret_names: ["FOREIGN_TOKEN"], runtime: { memory_mb: 256 } } }),
    boundProfile({ processor_id: "other_processor", workflow: { variables: { SECRET: "foreign-plain" }, configured_secret_names: ["FOREIGN_TOKEN"], runtime: { memory_mb: 256 } } }),
    boundProfile({ workflow: { variables: { "bad-name": "invalid-plain", ARRAY: [] }, configured_secret_names: ["<script>bad-name</script>", 4], runtime: { timeout_seconds: "90", memory_mb: 1000000 }, secrets: { API_TOKEN: "private-workflow-token" } } }),
  ]) {
    const p = page();
    await showBoundProfile(p, profile);
    assert.equal(p.node("#workflow-variable-0"), null);
    assert.doesNotMatch(p.node("#processor-configuration").innerHTML, /foreign-plain|FOREIGN_TOKEN|invalid-plain|private-workflow-token|<script>|<dd>1000000/);
  }
});

test("even a full product manager cannot load or bind shop processor accounts", async () => {
  const p = page({ role: "staff", canConfigure: true });
  await editProduct(p, product({ mode: "script", processor_id: "personalized_text", processor_config: { template: "payment-secret" } }));
  assert.equal(p.requests.length, 1);
  assert.equal(p.node("#p-processor").disabled, true);
  assert.equal(p.node("#p-profile"), null);
  assert.equal(p.node("#workflow-variable-0"), null);
  assert.equal(p.node("#pc-value-0"), null);
  assert.doesNotMatch(p.workspace.innerHTML, /payment-secret/);
  p.node("#product-form").emit("submit");
  assert.equal(p.requests[1].body.processor_id, "personalized_text");
  assert.equal(Object.hasOwn(p.requests[1].body, "processor_config"), false);
});

test("late processor-account metadata cannot replace a different editor", async () => {
  const p = page();
  await editProduct(p, product({ mode: "script", processor_id: "personalized_text" }));
  p.leave();
  p.workspace.innerHTML = "Different page";
  p.requests[1].resolve([{ id: "late", processor_id: "personalized_text", name: "Old account" }]);
  p.requests[2].resolve({ product_id: "product-one", profile: { id: "late", name: "Old account" } });
  await flush();
  assert.equal(p.workspace.innerHTML, "Different page");
});

test("a description editor has no fulfillment controls or editable output definitions", async () => {
  const p = page({ role: "staff", canConfigure: false });
  await editProduct(p);
  for (const id of ["p-mode", "p-delivery", "p-view", "p-url", "p-secret", "p-retry", "p-attempts", "p-processor", "o-key-0", "o-label-0", "o-description-0", "o-type-0"])
    assert.equal(p.node("#" + id).disabled, true, id);
  assert.equal(p.node("#p-name").disabled, false);
  assert.equal(p.node("#add-output").hidden, true);

  const processorPage = page({ role: "staff", canConfigure: false });
  await editProduct(processorPage, product({ mode: "script", processor_id: "personalized_text", processor_config: { template: "hidden secret" } }));
  assert.equal(processorPage.node("#pc-value-0"), null);
  assert.doesNotMatch(processorPage.workspace.innerHTML, /hidden secret/);
  assert.match(processorPage.node("#processor-configuration").innerHTML, /配置已隐藏/);
});

test("legacy products keep a stable default specification and submit prices as exact text", async () => {
  const p = page({ products: [product()] });
  await editProduct(p);
  assert.equal(p.node("#v-id-0").value, "default");
  assert.equal(p.node("#v-id-0").readOnly, true);
  p.node("#v-id-0").value = "changed-in-dom";
  p.node("#v-name-0").value = "两个月 · 加强版";
  p.node("#v-price-0").value = "999999999999.123456";
  p.node("#v-currency-0").value = "usdt";
  p.node("#v-attributes-0").value = '{"duration_months":2,"tier":"advanced","enabled_feature":true}';
  p.node("#product-form").emit("submit");
  const variant = JSON.parse(JSON.stringify(p.requests[1].body.variants[0]));
  assert.equal(variant.id, "default");
  assert.equal(variant.name, "两个月 · 加强版");
  assert.equal(variant.price, "999999999999.123456");
  assert.equal(variant.currency, "USDT");
  assert.deepEqual(variant.attributes, { duration_months: 2, tier: "advanced", enabled_feature: true });
  assert.equal(Object.hasOwn(variant, "stock"), false);
});

test("adding and deleting specifications retains earlier edits and keeps a default SKU", async () => {
  const p = page();
  await editProduct(p);
  p.node("#v-name-0").value = "基础版";
  p.node("#v-price-0").value = "19.90";
  await p.node("#add-variant").emit("click");
  assert.equal(p.node("#v-name-0").value, "基础版");
  assert.equal(p.node("#v-price-0").value, "19.90");
  assert.match(p.node("#v-id-1").value, /^sku_[a-z0-9_]+$/);
  assert.equal(p.node("#v-id-1").readOnly, true);
  assert.equal(p.node("#v-name-1").value, "基础版");
  assert.equal(p.node('[data-remove-variant="0"]'), null);
  await p.node('[data-remove-variant="1"]').emit("click");
  assert.equal(p.node("#v-id-1"), null);
  assert.equal(p.node("#v-name-0").value, "基础版");
});

test("invalid specification prices and nested or unsafe attributes block saving without losing the draft", async () => {
  for (const [selector, invalid] of [
    ["#v-price-0", "-1"],
    ["#v-price-0", "1e10"],
    ["#v-price-0", "1.1234567"],
    ["#v-price-0", "1000000000000"],
    ["#v-attributes-0", '{"nested":{"value":1}}'],
    ["#v-attributes-0", '{"account_id":9007199254740993}'],
  ]) {
    const p = page();
    await editProduct(p);
    p.node("#p-name").value = "保留这个名称";
    p.node(selector).value = invalid;
    await p.node("#product-form").emit("submit");
    await flush();
    assert.equal(p.requests.length, 1);
    assert.match(p.node("#error").textContent, /规格 1/);
    assert.equal(p.node("#p-name").value, "保留这个名称");
    assert.equal(p.node(selector).value, invalid);
    assert.equal(p.node("#save-product").disabled, false);
  }
});

test("an issued-SKU removal error preserves the editor and its specification draft", async () => {
  const value = product({ variants: [
    { id: "default", name: "默认规格", price: null, currency: "CNY", attributes: {}, enabled: true },
    { id: "advanced", name: "加强版", price: "29.9", currency: "CNY", attributes: { tier: "advanced" }, enabled: true },
  ] });
  const p = page({ products: [value] });
  await editProduct(p, value);
  await p.node('[data-remove-variant="1"]').emit("click");
  p.node("#product-form").emit("submit");
  p.requests[1].reject(new Error("已发行卡密的规格不能删除；请停用该规格：advanced"));
  await flush();
  assert.match(p.node("#error").textContent, /已发行卡密的规格不能删除/);
  assert.equal(p.node("#v-id-0").value, "default");
  assert.equal(p.node("#save-product").disabled, false);
});

test("copying saved metadata ignores unsaved editor changes and does not fetch unauthorized inventory", async () => {
  const written = [];
  const p = page({ role: "staff", clipboard: { async writeText(value) { written.push(value); return true; } } });
  await editProduct(p);
  p.node("#p-name").value = "尚未保存的名称";
  await p.node("#export-saved-product").emit("click");
  assert.equal(p.requests[1].url, "/manage/product");
  assert.equal(p.requests.length, 2);
  p.requests[1].resolve(product({ name: "已经保存的名称", webhook_secret: "secret-never-export", processor_config: { template: "delivery-never-export" } }));
  await flush();
  assert.equal(written.length, 1);
  assert.match(written[0], /已经保存的名称/);
  assert.doesNotMatch(written[0], /尚未保存的名称|secret-never-export|delivery-never-export/);
  assert.equal(p.node("#p-name").value, "尚未保存的名称");
  assert.equal(p.node("#product-export-text").readOnly, true);
  assert.equal(p.node("#product-export-text").value, written[0]);
  assert.match(p.node("#product-export-panel").innerHTML, /未提供卡密统计/);
});

test("scoped product copying includes only unredeemed-code counts and stops after navigation", async () => {
  for (const leaving of [false, true]) {
    const written = [];
    const p = page({ role: "staff", canManageCards: true, products: [product()], clipboard: { async writeText(value) { written.push(value); return false; } } });
    await p.ui.render(p.ctx);
    const copying = p.node('[data-export="product-one"]').emit("click");
    assert.equal(p.requests[0].url, "/manage/product");
    assert.equal(p.requests[1].url, "/manage/card-stats?product_id=product-one");
    if (leaving) {
      p.leave();
      p.workspace.innerHTML = "Different page";
    }
    p.requests[0].resolve(product());
    p.requests[1].resolve({ variants: [{ variant_id: "default", summary: { total: 12, remaining: 7, available: 8 }, cards: ["private-code"] }] });
    await copying;
    if (leaving) {
      assert.equal(written.length, 0);
      assert.equal(p.workspace.innerHTML, "Different page");
      assert.equal(p.notifications.length, 0);
    } else {
      assert.equal(written.length, 1);
      assert.match(written[0], /"remaining": 7/);
      assert.match(written[0], /未兑换卡密数量/);
      assert.doesNotMatch(written[0], /private-code/);
      assert.equal(p.node("#product-export-text").selected, true);
      assert.match(p.node("#product-export-panel").innerHTML, /不是未售库存/);
    }
  }
});

test("ordered processing steps keep stable identifiers, translated labels and the support mailbox", async () => {
  const value = product({ progress_steps: [
    { id: "verify", label: { "zh-CN": "核实信息", en: "Verify", fr: "Vérifier" } },
    { id: "deliver", label: { "zh-CN": "交付商品", en: "Deliver" } },
  ], support_email: "merchant@example.test" });
  const p = page({ products: [value] });
  await editProduct(p, value);
  assert.equal(p.node("#ps-id-0").readOnly, true);
  p.node("#ps-id-0").value = "tampered-id";
  p.node("#ps-label-zh-0").value = "核实账户";
  await p.node('[data-step-down="0"]').emit("click");
  assert.equal(p.node("#ps-id-0").value, "deliver");
  assert.equal(p.node("#ps-id-1").value, "verify");
  assert.equal(p.node("#ps-label-zh-1").value, "核实账户");
  await p.node("#add-progress-step").emit("click");
  const firstAdded = p.node("#ps-id-2").value;
  await p.node("#add-progress-step").emit("click");
  const lastAdded = p.node("#ps-id-3").value;
  assert.match(firstAdded, /^step_[a-z0-9_]+$/);
  assert.notEqual(firstAdded, lastAdded);
  await p.node('[data-step-remove="2"]').emit("click");
  assert.equal(p.node("#ps-id-2").value, lastAdded);
  p.node("#p-support-email").value = "followup@example.test";
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[1].body));
  assert.equal(body.progress_steps.length, 3);
  assert.equal(body.progress_steps[1].id, "verify");
  assert.deepEqual(body.progress_steps[1].label, { "zh-CN": "核实账户", en: "Verify", fr: "Vérifier" });
  assert.equal(body.support_email, "followup@example.test");
});

test("a blank processing-step label prevents saving while keeping the other draft fields", async () => {
  const p = page();
  await editProduct(p, product({ progress_steps: [{ id: "prepare", label: { "zh-CN": "准备", en: "Prepare" } }] }));
  p.node("#ps-label-zh-0").value = "";
  p.node("#ps-label-en-0").value = "";
  p.node("#p-name").value = "仍保留这个商品名称";
  await p.node("#product-form").emit("submit");
  await flush();
  assert.equal(p.requests.length, 1);
  assert.match(p.node("#error").textContent, /步骤 1/);
  assert.equal(p.node("#p-name").value, "仍保留这个商品名称");
  assert.equal(p.node("#save-product").disabled, false);
});

test("products cannot add a thirty-first processing step", async () => {
  const value = product({ progress_steps: Array.from({ length: 30 }, (_, index) => ({
    id: "phase_" + index,
    label: { "zh-CN": "步骤 " + index, en: "Step " + index },
  })) });
  const p = page();
  await editProduct(p, value);
  await p.node("#add-progress-step").emit("click");
  await flush();
  assert.equal(p.node("#ps-id-30"), null);
  assert.match(p.node("#error").textContent, /最多支持 30 个处理步骤/);
});


test("rich field definitions keep stable choices and bounded image collections", async () => {
  const p = page();
  await editProduct(p, product({ parameters: [fieldDefinition("tier")], outputs: [fieldDefinition("pictures", "images")] }));
  p.node("#f-type-0").value = "select";
  await p.node("#f-type-0").emit("change");
  assert.equal(p.node("#f-options-section-0").hidden, false);
  assert.equal(p.node("#f-images-section-0").hidden, true);
  assert.equal(p.node("#f-options-0").disabled, false);
  assert.equal(p.node("#f-max-items-0").disabled, true);
  const choices = [{ value: "basic", label: { "zh-CN": "基础版", en: "Basic" } }, { value: "pro", label: { "zh-CN": "加强版" } }];
  p.node("#f-options-0").value = JSON.stringify(choices);
  p.node("#o-max-items-0").value = "4";
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[1].body));
  assert.deepEqual(body.parameters[0].options, choices);
  assert.equal(body.outputs[0].max_items, 4);
  assert.equal(body.outputs[0].type, "images");
  assert.equal(Object.hasOwn(body.parameters[0], "max_items"), false);
  assert.equal(Object.hasOwn(body.outputs[0], "options"), false);
});

test("changing rich field types removes obsolete metadata while preserving the draft", async () => {
  const p = page();
  await editProduct(p, product({ parameters: [{ ...fieldDefinition("tier", "select"), options: [{ value: "basic", label: { "zh-CN": "基础版" } }] }], outputs: [{ ...fieldDefinition("pictures", "images"), max_items: 3 }] }));
  p.node("#f-type-0").value = "boolean";
  await p.node("#f-type-0").emit("change");
  p.node("#o-type-0").value = "image";
  await p.node("#o-type-0").emit("change");
  assert.equal(p.node("#f-options-section-0").hidden, true);
  assert.equal(p.node("#o-images-section-0").hidden, true);
  assert.equal(p.node("#o-max-items-0").disabled, true);
  assert.equal(p.node("#f-options-0").disabled, true);
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[1].body));
  assert.equal(body.parameters[0].type, "boolean");
  assert.equal(body.outputs[0].type, "image");
  assert.equal(Object.hasOwn(body.parameters[0], "options"), false);
  assert.equal(Object.hasOwn(body.outputs[0], "max_items"), false);
});

test("invalid rich definitions block saving and keep typed schema drafts", async () => {
  for (const options of ["not JSON", "[]", '[{"value":"same","label":{"zh-CN":"一"}},{"value":"same","label":{"zh-CN":"二"}}]', '[{"value":"不能用显示名称","label":{"zh-CN":"名称"}}]']) {
    const p = page();
    await editProduct(p, product({ parameters: [fieldDefinition("tier", "select")] }));
    p.node("#f-options-0").value = options;
    p.node("#p-name").value = "保留的商品草稿";
    await p.node("#product-form").emit("submit");
    assert.equal(p.requests.length, 1);
    assert.match(p.node("#error").textContent, /下拉选项/);
    assert.equal(p.node("#f-options-0").value, options);
    assert.equal(p.node("#p-name").value, "保留的商品草稿");
  }
  for (const limit of ["0", "21", "2.5", ""]) {
    const p = page();
    await editProduct(p, product({ outputs: [fieldDefinition("pictures", "images")] }));
    p.node("#o-max-items-0").value = limit;
    await p.node("#product-form").emit("submit");
    assert.equal(p.requests.length, 1);
    assert.match(p.node("#error").textContent, /1–20/);
    assert.equal(p.node("#o-max-items-0").value, limit);
  }
});

test("workshop slogans are readable and editable without delivery configuration permission", async () => {
  const value = product({ workshop_slogan: '认真制作\n<script>content only</script>' });
  const p = page({ role: "staff", canEdit: true, canConfigure: false, products: [value] });
  await editProduct(p, value);
  assert.equal(p.node("#p-workshop-slogan").value, value.workshop_slogan);
  assert.equal(p.node("#p-workshop-slogan").disabled, false);
  assert.match(p.workspace.innerHTML, /车间标语 · 给 AI 的工作提示/);
  assert.doesNotMatch(p.workspace.innerHTML, /<script>content only<\/script>/);
  p.node("#p-workshop-slogan").value = "核验来源，然后交付";
  p.node("#product-form").emit("submit");
  assert.equal(p.requests[1].body.workshop_slogan, "核验来源，然后交付");
  assert.equal(p.requests[1].url, "/manage/product");
  p.leave(); p.requests[1].resolve(value); await flush();
  assert.equal(p.saved.length, 0);
});

test("修改权益使用商家选择的任意属性键，保存多语言显示名并允许缺失额度为零", async () => {
  const value = product({ variants: [{ id: "default", name: "标准", price: "25", currency: "CNY", attributes: {}, enabled: true }, { id: "plus", name: "加强", price: "50", currency: "CNY", attributes: { document_edits: 1 }, enabled: true }] });
  const p = page({ products: [value] });
  await editProduct(p, value);
  p.node("#p-revisions-enabled").checked = true;
  await p.node("#p-revisions-enabled").emit("change");
  assert.equal(p.node("#revision-policy-fields").hidden, false);
  p.node("#p-revision-key").value = "document_edits";
  p.node("#p-revision-label").value = '{"zh-CN":"成品修改机会","en":"Included edits"}';
  p.node("#product-form").emit("submit");
  const body = JSON.parse(JSON.stringify(p.requests[1].body));
  assert.deepEqual(body.revision_policy, { attribute_key: "document_edits", label: { "zh-CN": "成品修改机会", en: "Included edits" } });
  assert.deepEqual(body.variants[0].attributes, {});
  assert.deepEqual(body.variants[1].attributes, { document_edits: 1 });
});

test("修改权益拒绝负数、字符串、布尔值、null 和小数额度", async () => {
  for (const allowance of [-1, "1", true, null, 1.5]) {
    const value = product({ revision_policy: { attribute_key: "custom_quota", label: { "zh-CN": "额度" } }, variants: [{ id: "default", name: "基础", attributes: { custom_quota: allowance } }] });
    const p = page();
    await editProduct(p, value);
    await p.node("#product-form").emit("submit");
    await flush();
    assert.equal(p.requests.length, 1);
    assert.match(p.node("#error").textContent, /必须是非负整数/);
  }
});

test("关闭修改权益提交 null，不能给单次查看或服务交付启用权益", async () => {
  const value = product({ revision_policy: { attribute_key: "custom_quota", label: { "zh-CN": "额度" } } });
  const p = page();
  await editProduct(p, value);
  p.node("#p-revisions-enabled").checked = false;
  await p.node("#p-revisions-enabled").emit("change");
  assert.equal(p.node("#revision-policy-fields").hidden, true);
  p.node("#product-form").emit("submit");
  assert.equal(p.requests[1].body.revision_policy, null);
  for (const mode of [{ view_policy: "once" }, { delivery: "service", outputs: [] }, { mode: "stock", parameters: [] }]) {
    const other = page();
    await editProduct(other, { ...value, ...mode });
    await other.node("#product-form").emit("submit");
    await flush();
    assert.equal(other.requests.length, 1);
    assert.match(other.node("#error").textContent, /修改权益需要可重复查看/);
  }
});

test("缺少发货配置权限的商品管理页面不允许编辑修改策略", async () => {
  const p = page({ role: "staff", canConfigure: false });
  await editProduct(p, product({ revision_policy: { attribute_key: "edits", label: { "zh-CN": "修改次数" } } }));
  assert.equal(p.node("#p-revisions-enabled").disabled, true);
  assert.equal(p.node("#p-revision-key").disabled, true);
  assert.equal(p.node("#p-revision-label").disabled, true);
});

test("纯商品编辑权限可改展示属性，但不能改变策略选中的修改额度", async () => {
  const value = product({ revision_policy: { attribute_key: "edit_allowance", label: { "zh-CN": "修改次数" } }, variants: [{ id: "default", name: "强化版", attributes: { edit_allowance: 1, style: "simple" } }] });
  const allowed = page({ role: "staff", canConfigure: false });
  await editProduct(allowed, value);
  allowed.node("#v-attributes-0").value = '{"edit_allowance":1,"style":"professional"}';
  allowed.node("#product-form").emit("submit");
  assert.equal(allowed.requests[1].body.variants[0].attributes.style, "professional");
  const denied = page({ role: "staff", canConfigure: false });
  await editProduct(denied, value);
  denied.node("#v-attributes-0").value = '{"edit_allowance":2,"style":"professional"}';
  await denied.node("#product-form").emit("submit");
  await flush();
  assert.equal(denied.requests.length, 1);
  assert.match(denied.node("#error").textContent, /修改卡密属性 edit_allowance 的额度需要配置发货权限/);
  assert.equal(denied.node("#v-attributes-0").value, '{"edit_allowance":2,"style":"professional"}');
});
