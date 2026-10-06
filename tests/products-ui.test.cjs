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

async function renderOwner(p) {
  const rendering = p.ui.render(p.ctx);
  assert.equal(p.requests[0].url, "/admin/product-templates");
  p.requests[0].resolve(templates);
  await rendering;
}

async function editProduct(p, value = product()) {
  const editing = p.ui.edit(p.ctx, value);
  assert.equal(p.requests[0].url, p.ctx.role === "staff" ? "/manage/processors" : "/admin/processors");
  p.requests[0].resolve(catalog);
  await editing;
}

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

test("even a full product manager cannot load or bind shop processor accounts", async () => {
  const p = page({ role: "staff", canConfigure: true });
  await editProduct(p, product({ mode: "script", processor_id: "personalized_text", processor_config: { template: "payment-secret" } }));
  assert.equal(p.requests.length, 1);
  assert.equal(p.node("#p-processor").disabled, true);
  assert.equal(p.node("#p-profile"), null);
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
