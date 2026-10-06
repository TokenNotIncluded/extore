const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs
  .readFileSync(path.join(__dirname, "../extore/static/app.js"), "utf8")
  .replace(/start\(\);\s*$/, "");
const productSource = fs.readFileSync(
  path.join(__dirname, "../extore/static/products.js"),
  "utf8",
);
const fullLink =
  "https://extore.lmm.best/staff?source=merchant&product=one#Complete-secret_TOKEN";
const product = { id: "product-one", name: "测试商品" };
const flush = () => new Promise((resolve) => setImmediate(resolve));

function page(options = {}) {
  const nodes = new Map();
  const requests = [];
  let anonymous = 0;
  const decode = (value) =>
    String(value)
      .replaceAll("&quot;", '"')
      .replaceAll("&#39;", "'")
      .replaceAll("&lt;", "<")
      .replaceAll("&gt;", ">")
      .replaceAll("&amp;", "&");
  const contains = (ancestor, item) => {
    for (let current = item.parent; current; current = current.parent)
      if (current === ancestor) return true;
    return false;
  };

  function element(id, parent = null) {
    const listeners = new Map();
    const item = {
      id,
      parent,
      value: "",
      checked: false,
      disabled: false,
      readOnly: false,
      hidden: true,
      textContent: "",
      attributes: {},
      dataset: {},
      style: {},
      isConnected: true,
      focus() {
        this.focused = true;
      },
      select() {
        this.selected = true;
        this.selectionStart = 0;
        this.selectionEnd = this.value.length;
      },
      setSelectionRange(start, end) {
        this.selectionStart = start;
        this.selectionEnd = end;
      },
      setAttribute(name, value) {
        this.attributes[name] = value;
        if (name === "readonly") this.readOnly = true;
        if (name === "disabled") this.disabled = true;
      },
      addEventListener(name, handler) {
        listeners.set(name, [...(listeners.get(name) || []), handler]);
      },
      async emit(name) {
        const event = { target: item, preventDefault() {} };
        for (const handler of listeners.get(name) || []) await handler(event);
      },
      querySelector: (selector) => find(selector, item),
      querySelectorAll: (selector) => findAll(selector, item),
    };
    let markup = "";
    Object.defineProperty(item, "innerHTML", {
      get: () => markup,
      set(value) {
        markup = String(value);
        for (const [childId, child] of nodes) {
          if (contains(item, child)) {
            child.isConnected = false;
            nodes.delete(childId);
          }
        }
        parse(markup, item);
      },
    });
    return item;
  }

  function parse(markup, parent) {
    for (const tag of markup.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
      const [, kind, source] = tag;
      const id = source.match(/\bid="([^"]+)"/)?.[1];
      if (!id && !/\b(?:name|data-[a-z-]+|type)="/i.test(source)) continue;
      const item = element(id || `anonymous-${++anonymous}`, parent);
      item.kind = kind;
      nodes.set(item.id, item);
      for (const attribute of source.matchAll(/\b([a-z][a-z-]*)="([^"]*)"/gi)) {
        const name = attribute[1];
        const value = decode(attribute[2]);
        item.attributes[name] = value;
        if (name === "value") item.value = value;
        if (name.startsWith("data-")) {
          const key = name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
          item.dataset[key] = value;
        }
      }
      item.disabled = /(?:^|\s)disabled(?:\s|=|$)/i.test(source);
      item.readOnly = /(?:^|\s)readonly(?:\s|=|$)/i.test(source);
      item.checked = /(?:^|\s)checked(?:\s|=|$)/i.test(source);
      if (kind === "select") {
        const body = markup.slice(tag.index + tag[0].length).split("</select>")[0];
        const choices = [...body.matchAll(/<option\b([^>]*)>/gi)];
        const selected = choices.find((choice) => /\bselected\b/i.test(choice[1])) || choices[0];
        item.value = decode(selected?.[1].match(/\bvalue="([^"]*)"/)?.[1] || "");
      }
    }
  }

  function findAll(selector, root = null) {
    if (selector.includes(","))
      return [...new Set(selector.split(",").flatMap((part) => findAll(part, root)))];
    return [...nodes.values()].filter((item) => {
      if (root && !contains(root, item)) return false;
      if (selector.endsWith(":checked") && !item.checked) return false;
      const plain = selector.replace(/:checked$/, "");
      const id = plain.match(/^#([\w-]+)$/);
      if (id) return item.id === id[1];
      if (plain === "#form button[type=submit]")
        return item.kind === "button" && item.attributes.type === "submit";
      const attributes = [...plain.matchAll(/\[([a-z-]+)(?:="([^"]*)")?\]/gi)];
      return attributes.length > 0 && attributes.every(([, name, value]) =>
        Object.hasOwn(item.attributes, name) &&
          (value === undefined || item.attributes[name] === value),
      );
    });
  }
  function find(selector, root = null) {
    return findAll(selector, root)[0] || null;
  }
  for (const id of ["app", "brand", "theme", "language", "header-context", "toast", "workspace"])
    nodes.set(id, element(id));
  for (const [id, values] of [["theme", ["auto", "light", "dark"]], ["language", ["auto"]]]) {
    for (const value of values) {
      const option = element(`${id}-${value}`, nodes.get(id));
      option.attributes.value = value;
      nodes.set(option.id, option);
    }
  }
  const originalFind = find;
  const query = (selector) => {
    const option = selector.match(/^#(theme|language) option\[value=(\w+)\]$/);
    return option ? nodes.get(`${option[1]}-${option[2]}`) : originalFind(selector);
  };
  const location = {
    pathname: "/admin",
    hash: "",
    origin: "https://extore.lmm.best",
  };
  const browser = {
    addEventListener() {},
    ExtorePreferences: {
      settings: { theme: "auto", language: "auto" },
      resolved: { theme: "light", language: options.language || "zh-CN" },
      subscribe() {},
    },
  };
  const context = vm.createContext({
    window: browser,
    document: { querySelector: query, querySelectorAll: findAll },
    location,
    navigator: options.navigator || {},
    isSecureContext: options.isSecureContext,
    history: { pushState() {}, replaceState() {} },
    localStorage: { getItem: () => null, setItem() {} },
    DOMPurify: { sanitize: (value) => value },
    marked: { parse: (value) => value },
    URL,
    URLSearchParams,
    setTimeout: () => 1,
    clearTimeout() {},
    fetch: async (url, request) => {
      const body = request.body ? JSON.parse(request.body) : undefined;
      const entry = { url, body, method: request.method };
      requests.push(entry);
      if (options.deferRequest?.(url, request)) {
        return new Promise((resolve, reject) => {
          entry.respond = (data) => resolve({ ok: true, json: async () => data });
          entry.reject = reject;
        });
      }
      let data;
      if (request.method === "POST") data = { url: fullLink };
      else if (url === "/api/manage/products") data = [product];
      else if (["/api/admin/staff", "/api/manage/links", "/api/admin/product-templates"].includes(url)) data = [];
      else throw new Error(`Unexpected request: ${url}`);
      return { ok: true, json: async () => data };
    },
  });
  vm.runInContext(appSource, context);
  return {
    context,
    window: browser,
    node: query,
    requests,
    input() {
      const input = element("manual-link");
      input.value = fullLink;
      input.readOnly = true;
      return input;
    },
    enter(nextRole = "admin", nextTab = "staff") {
      location.pathname = nextRole === "staff" ? "/staff" : "/admin";
      context.testAuth = {
        role: nextRole,
        permissions: ["queue.view", "queue.process", "links.delegate"],
        product_id: nextRole === "staff" ? product.id : null,
        link_expires: Date.now() / 1000 + 14 * 86400,
      };
      context.testProducts = [product];
      context.testTab = nextTab;
      vm.runInContext("acceptAuth(testAuth); products = testProducts; tab = testTab", context);
    },
    loadProducts() {
      vm.runInContext(productSource, context);
    },
  };
}

test("shared clipboard helper copies the exact management URL, including its token", async () => {
  for (const isSecureContext of [true, undefined]) {
    const writes = [];
    const p = page({
      isSecureContext,
      navigator: { clipboard: { async writeText(value) { writes.push(value); } } },
    });
    assert.equal(await p.window.ExtoreClipboard.writeText(fullLink), true);
    assert.deepEqual(writes, [fullLink]);
  }
});

test("shared clipboard helper safely reports insecure, missing, or denied clipboard access", async () => {
  let insecureWrites = 0;
  const cases = [
    { isSecureContext: false, navigator: { clipboard: { async writeText() { insecureWrites++; } } } },
    { isSecureContext: true },
    { isSecureContext: true, navigator: { clipboard: {} } },
    { isSecureContext: true, navigator: { clipboard: { async writeText() { throw new Error("permission denied"); } } } },
  ];
  for (const options of cases)
    assert.equal(await page(options).window.ExtoreClipboard.writeText(fullLink), false);
  assert.equal(insecureWrites, 0);
});

test("management copy reports success in the selected language and preserves the readonly link", async () => {
  for (const [language, message] of [["zh-CN", "链接已复制"], ["en", "Link copied"]]) {
    const writes = [];
    const p = page({ language, navigator: { clipboard: { async writeText(value) { writes.push(value); } } } });
    const input = p.input();
    assert.equal(await p.context.copyManagementLink(fullLink, input), true);
    assert.deepEqual(writes, [fullLink]);
    assert.equal(p.node("#toast").textContent, message);
    assert.equal(input.readOnly, true);
    assert.equal(input.value, fullLink);
    assert.equal(input.focused, undefined);
  }
});

test("management copy denial selects the complete readonly link for manual copying", async () => {
  for (const [language, message] of [
    ["zh-CN", "复制失败，已选中链接，请手动复制。"],
    ["en", "Could not copy. The link is selected; copy it manually."],
  ]) {
    const p = page({ language, navigator: { clipboard: { async writeText() { throw new Error("denied"); } } } });
    const input = p.input();
    assert.equal(await p.context.copyManagementLink(fullLink, input), false);
    assert.equal(input.focused, true);
    assert.equal(input.selected, true);
    assert.equal(input.selectionStart, 0);
    assert.equal(input.selectionEnd, fullLink.length);
    assert.equal(input.value, fullLink);
    assert.equal(input.readOnly, true);
    assert.equal(p.node("#toast").textContent, message);
  }
});

test("management copy without an input still gives a localized manual-copy instruction", async () => {
  for (const [language, message] of [
    ["zh-CN", "复制失败，请手动复制链接。"],
    ["en", "Could not copy. Copy the link manually."],
  ]) {
    const p = page({ language });
    assert.equal(await p.context.copyManagementLink(fullLink, null), false);
    assert.equal(p.node("#toast").textContent, message);
  }
});

test("clipboard completion after navigation leaves the new page untouched", async () => {
  for (const succeed of [true, false]) {
    let finish;
    let active = true;
    const p = page({ navigator: { clipboard: { writeText: () => new Promise((resolve, reject) => {
      finish = () => succeed ? resolve() : reject(new Error("denied"));
    }) } } });
    const input = p.input();
    p.node("#toast").textContent = "A current-page notification";
    const copying = p.context.copyManagementLink(fullLink, input, () => active);
    active = false;
    finish();
    assert.equal(await copying, succeed);
    assert.equal(input.focused, undefined);
    assert.equal(input.selected, undefined);
    assert.equal(input.readOnly, true);
    assert.equal(p.node("#toast").textContent, "A current-page notification");
    assert.equal(p.node("#toast").hidden, true);
  }
});

test("owner and delegated creation both expose a copy button without changing link permissions", async () => {
  for (const nextRole of ["admin", "staff"]) {
    const writes = [];
    const p = page({ navigator: { clipboard: { async writeText(value) { writes.push(value); } } } });
    p.enter(nextRole);
    await p.context.renderStaff();
    p.node("#staff-name").value = "Queue worker";
    p.node("#staff-product").value = product.id;
    p.node("#staff-days").value = "3";
    p.node("#staff-max-uses").value = "1";
    await p.node("#form").emit("submit");
    await flush();
    const creation = p.requests.find((request) => request.method === "POST");
    assert.equal(creation.url, nextRole === "admin" ? "/api/admin/staff" : "/api/manage/links");
    assert.deepEqual(creation.body, {
      name: "Queue worker",
      product_id: product.id,
      days: 3,
      max_uses: 1,
      permissions: ["queue.view", "queue.process"],
    });
    const input = p.node("#created-management-link");
    assert.ok(input, "created link input should exist");
    assert.equal(input.value, fullLink);
    assert.equal(input.readOnly, true);
    assert.ok(p.node("#copy-management-link"), "created link copy button should exist");
    assert.match(p.node("#staff-link").innerHTML, /复制链接/);
    await p.node("#copy-management-link").emit("click");
    await flush();
    assert.deepEqual(writes, [fullLink]);
    assert.equal(p.node("#toast").textContent, "链接已复制");
  }
});

test("a pending rendered copy checks its route, tab, role, generation, and original input", async () => {
  const changes = [
    (p) => { p.context.location.pathname = "/"; },
    (p) => vm.runInContext('tab = "products"', p.context),
    (p) => vm.runInContext('role = "staff"', p.context),
    (p) => vm.runInContext("queueLoadId++", p.context),
    (p) => { p.node("#workspace").innerHTML = "Current page"; },
  ];
  for (const change of changes) {
    let rejectWrite;
    const p = page({ navigator: { clipboard: { writeText: () => new Promise((_resolve, reject) => { rejectWrite = reject; }) } } });
    p.enter();
    await p.context.renderStaff();
    p.node("#staff-name").value = "Queue worker";
    p.node("#staff-days").value = "3";
    p.node("#staff-max-uses").value = "1";
    await p.node("#form").emit("submit");
    await flush();
    const input = p.node("#created-management-link");
    const copying = p.node("#copy-management-link").emit("click");
    change(p);
    const markup = p.node("#workspace").innerHTML;
    p.node("#toast").textContent = "Current notification";
    rejectWrite(new Error("denied"));
    await copying;
    assert.equal(input.focused, undefined);
    assert.equal(input.selected, undefined);
    assert.equal(p.node("#workspace").innerHTML, markup);
    assert.equal(p.node("#toast").textContent, "Current notification");
  }
});

test("a post-creation management refresh cannot replace a different route or tab", async () => {
  for (const change of [
    (p) => { p.context.location.pathname = "/"; },
    (p) => vm.runInContext('tab = "products"', p.context),
  ]) {
    let delayRefresh = false;
    const p = page({ deferRequest: (url, request) =>
      delayRefresh && url === "/api/admin/staff" && request.method === "GET",
    });
    p.enter();
    await p.context.renderStaff();
    p.node("#staff-name").value = "Queue worker";
    p.node("#staff-days").value = "3";
    p.node("#staff-max-uses").value = "1";
    delayRefresh = true;
    await p.node("#form").emit("submit");
    await flush();
    const refresh = p.requests.at(-1);
    assert.equal(refresh.url, "/api/admin/staff");
    assert.equal(refresh.method, "GET");
    assert.equal(typeof refresh.respond, "function");
    assert.equal(p.requests.filter((request) => request.method === "POST").length, 1);
    change(p);
    p.node("#workspace").innerHTML = "Another workspace";
    refresh.respond([]);
    await flush();
    assert.equal(p.node("#workspace").innerHTML, "Another workspace");
    assert.equal(p.node("#created-management-link"), null);
    assert.equal(p.node("#staff-link"), null);
  }
});

test("a delegated product read stops before reading links when its management page is left", async () => {
  for (const change of [
    (p) => { p.context.location.pathname = "/"; },
    (p) => vm.runInContext('tab = "products"', p.context),
  ]) {
    const p = page({ deferRequest: (url) => url === "/api/manage/products" });
    p.enter("staff");
    const rendering = p.context.renderStaff();
    assert.equal(p.requests.length, 1);
    assert.equal(p.requests[0].url, "/api/manage/products");
    change(p);
    p.node("#workspace").innerHTML = "Another workspace";
    p.requests[0].respond([product]);
    await rendering;
    assert.equal(p.requests.length, 1, "stale product read must not start a management-link read");
    assert.equal(p.node("#workspace").innerHTML, "Another workspace");
  }
});

test("quick draft configuration links use the shared management-copy callback", async () => {
  const writes = [];
  const p = page({ language: "en", navigator: { clipboard: { async writeText(value) { writes.push(value); } } } });
  p.enter("admin", "products");
  p.loadProducts();
  const ctx = p.context.productUIContext();
  assert.equal(typeof ctx.copyManagementLink, "function");
  let called;
  const shared = ctx.copyManagementLink;
  ctx.copyManagementLink = async (...args) => {
    called = args;
    return shared(...args);
  };
  await p.window.ExtoreProducts.render(ctx, {
    productId: product.id,
    productName: product.name,
    url: fullLink,
  });
  const input = p.node("#quick-management-link");
  assert.equal(input.readOnly, true);
  assert.equal(input.value, fullLink);
  await p.node("#copy-quick-link").emit("click");
  await flush();
  assert.ok(called, "quick links should delegate to the shared callback");
  assert.equal(called[0], fullLink);
  assert.equal(called[1], input);
  assert.equal(called[2](), true);
  assert.deepEqual(writes, [fullLink]);
  assert.equal(p.node("#toast").textContent, "Link copied");
});
