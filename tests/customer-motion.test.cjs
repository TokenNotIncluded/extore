const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/customer-motion.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "../extore/static/app.js"), "utf8");
const flush = async () => {
  for (let index = 0; index < 8; index++) await Promise.resolve();
};

function fixture({ reduced = false, hidden = false, mobile = false, intersection = true } = {}) {
  const animations = [];
  const observers = [];
  class Events {
    constructor() { this.listeners = new Map(); }
    addEventListener(type, handler) {
      if (!this.listeners.has(type)) this.listeners.set(type, new Set());
      this.listeners.get(type).add(handler);
    }
    removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
    emit(type, event = {}) {
      for (const handler of this.listeners.get(type) || []) handler(event);
    }
  }
  class Element extends Events {
    constructor(classes = [], tagName = "div") {
      super();
      const names = new Set(classes);
      this.classList = {
        add: (...values) => values.forEach((value) => names.add(value)),
        remove: (...values) => values.forEach((value) => names.delete(value)),
        contains: (value) => names.has(value),
        toggle(value, enabled) {
          if (enabled) names.add(value); else names.delete(value);
        },
      };
      this.isConnected = true;
      this.style = { visibility: "" };
      this.attributes = new Map();
      this.children = [];
      this.selectors = new Map();
      this.dataset = {};
      this.tagName = tagName.toUpperCase();
      this.parent = null;
      this.rect = { left: 50, top: 50, right: 450, bottom: 350, width: 400, height: 300 };
      this.offsetLeft = 0;
      this.offsetWidth = 350;
      this.scrollLeft = 0;
      this.clientWidth = 386;
      this.scrolls = [];
      this.focuses = [];
    }
    querySelector(selector) { return this.selectors.get(selector) || null; }
    querySelectorAll(selector) { return this.selectors.get(selector) || []; }
    closest(selector) {
      if (selector === "[data-home-paper]") {
        if (this.dataset.homePaper) return this;
      } else if (["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "LABEL", "SUMMARY"].includes(this.tagName)
        || this.attributes.has("contenteditable") || this.attributes.get("role") === "button") return this;
      return this.parent?.closest(selector) || null;
    }
    getBoundingClientRect() { return this.rect; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    append(node) { this.children.push(node); }
    remove() { this.isConnected = false; }
    cloneNode() { return new Element(); }
    scrollTo(options) { this.scrolls.push(options); this.scrollLeft = options.left; }
    focus(options) { this.focuses.push(options); }
    animate(frames, options) {
      let resolve, reject;
      const finished = new Promise((ok, fail) => { resolve = ok; reject = fail; });
      const animation = { frames, options, finished, resolve, cancel() { reject(new Error("Canceled")); } };
      animations.push(animation);
      return animation;
    }
  }
  const document = new Events();
  document.hidden = hidden;
  document.createElement = () => new Element();
  const reduceMedia = Object.assign(new Events(), { matches: reduced });
  const mobileMedia = Object.assign(new Events(), { matches: mobile });
  const window = Object.assign(new Events(), {
    innerWidth: mobile ? 390 : 1280,
    innerHeight: 900,
    matchMedia: (query) => query.includes("reduced-motion") ? reduceMedia : mobileMedia,
  });
  if (intersection) window.IntersectionObserver = class {
    constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
    observe(node) { this.targets.push(node); }
    disconnect() { this.targets = []; }
  };
  vm.runInNewContext(source, { window, document });
  const app = new Element();
  const exchange = new Element(["exchange"]);
  const receipt = new Element(["receipt-waiting", "narrow"]);
  app.selectors.set(".exchange", exchange);
  app.selectors.set(".receipt-waiting", receipt);
  const render = () => {
    app.selectors.delete(".exchange");
    app.selectors.set(".narrow", receipt);
  };
  return { module: window.ExtoreMotion, Element, app, exchange, receipt, render, animations, observers, window, document, reduceMedia, mobileMedia };
}

test("纸张分离后才渲染下一页，完成后释放临时动画层", async () => {
  const page = fixture();
  let renders = 0;
  const pending = page.module.transition(page.app, () => { renders++; page.render(); });
  assert.equal(page.animations.length, 2);
  assert.ok(page.animations.every((animation) => animation.options.duration === 350));
  assert.equal(renders, 0);
  assert.equal(page.exchange.style.visibility, "hidden");
  const layer = page.app.children[0];
  assert.equal(layer.attributes.get("aria-hidden"), "true");
  assert.equal(layer.inert, true);
  page.animations[0].resolve();
  page.animations[1].resolve();
  await flush();
  assert.equal(renders, 1);
  assert.equal(page.animations.length, 3);
  page.animations[2].resolve();
  assert.equal(await pending, true);
  assert.equal(layer.isConnected, false);
  assert.equal(page.exchange.style.visibility, "");
});

test("降低动态效果、隐藏页面和没有 WAAPI 时直接渲染", async () => {
  for (const options of [{ reduced: true }, { hidden: true }, {}]) {
    const page = fixture(options);
    if (!options.reduced && !options.hidden) page.exchange.animate = undefined;
    let renders = 0;
    assert.equal(await page.module.transition(page.app, () => { renders++; }), true);
    assert.equal(renders, 1);
    assert.equal(page.animations.length, 0);
  }
});

test("显式取消和过期页面均不渲染旧流程，原纸卡恢复可见", async () => {
  const page = fixture();
  let renders = 0;
  const pending = page.module.transition(page.app, () => { renders++; });
  page.module.cancel(page.app);
  assert.equal(await pending, false);
  assert.equal(renders, 0);
  assert.equal(page.exchange.style.visibility, "");
  assert.equal(await page.module.transition(page.app, () => { renders++; }, () => false), false);
  assert.equal(renders, 0);
});

test("第二次过渡中断第一次，只有最新过渡可以渲染", async () => {
  const page = fixture();
  const renders = [];
  const first = page.module.transition(page.app, () => renders.push("old"));
  const second = page.module.transition(page.app, () => renders.push("new"));
  page.animations[2].resolve();
  page.animations[3].resolve();
  assert.equal(await first, false);
  assert.equal(await second, true);
  assert.deepEqual(renders, ["new"]);
});

test("等待动画复用实例，隐藏、离屏、用户暂停与减少动态都会暂停", () => {
  const page = fixture();
  const controller = page.module.mount(page.app);
  assert.equal(page.module.mount(page.app), controller);
  assert.equal(page.observers.length, 1);
  assert.equal(page.app.classList.contains("motion-paused"), true);
  const observation = page.observers[0];
  observation.callback([{ target: page.receipt, isIntersecting: true, intersectionRatio: 1 }]);
  assert.equal(page.app.classList.contains("motion-paused"), false);
  page.module.setPaused(page.receipt, true);
  assert.equal(controller.paused, true);
  assert.equal(page.app.classList.contains("motion-paused"), true);
  controller.setPaused(false);
  page.document.hidden = true;
  page.document.emit("visibilitychange");
  assert.equal(page.app.classList.contains("motion-paused"), true);
  page.document.hidden = false;
  page.reduceMedia.matches = true;
  page.reduceMedia.emit("change");
  assert.equal(page.app.classList.contains("motion-paused"), true);
  page.reduceMedia.matches = false;
  observation.callback([{ target: page.receipt, isIntersecting: false, intersectionRatio: 0 }]);
  assert.equal(page.app.classList.contains("motion-paused"), true);
  controller.dispose();
  assert.equal(page.document.listeners.get("visibilitychange").size, 0);
  assert.equal(observation.targets.length, 0);
});

test("没有 IntersectionObserver 时仅在滚动/窗口变化检测可见性，无持续计时器", () => {
  const page = fixture({ intersection: false });
  page.receipt.rect.top = 2000;
  page.receipt.rect.bottom = 2300;
  const controller = page.module.mount(page.app);
  assert.equal(page.app.classList.contains("motion-paused"), true);
  page.receipt.rect.top = 50;
  page.receipt.rect.bottom = 350;
  page.window.emit("scroll");
  assert.equal(page.app.classList.contains("motion-paused"), false);
  controller.dispose();
  assert.equal(page.window.listeners.get("scroll").size, 0);
});

test("等待纸条只有几何装饰，不伪造处理百分比或插入语言字符串", () => {
  const page = fixture();
  const markup = page.module.waitingMarkup('<script>alert("x")</script>');
  assert.match(markup, /aria-hidden="true"/);
  assert.equal((markup.match(/class="waiting-slip"/g) || []).length, 6);
  assert.doesNotMatch(markup, /<script|%|progressbar|canvas|video/i);
  assert.match(page.module.waitingMarkup("en"), /data-motion-language="en"/);
});

function home(page) {
  const stack = new page.Element(["home-paper-stack"]);
  const products = new page.Element();
  products.dataset.homePaper = "products";
  products.parent = stack;
  products.offsetLeft = 18;
  const redeem = new page.Element();
  redeem.dataset.homePaper = "redeem";
  redeem.parent = stack;
  redeem.offsetLeft = 388;
  stack.selectors.set("[data-home-paper]", [products, redeem]);
  page.app.selectors.set(".home-paper-stack", stack);
  return { stack, products, redeem };
}

test("桌面初始双卡没有预选，第一次点击纸卡内容即切换并聚焦该纸卡", () => {
  const page = fixture();
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  assert.equal(page.module.mountHome(page.app), controller);
  assert.equal(cards.stack.classList.contains("home-focus-products"), false);
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), false);
  assert.equal(cards.products.attributes.get("aria-current"), "false");
  assert.equal(cards.redeem.attributes.get("aria-current"), "false");
  const heading = new page.Element([], "h1");
  heading.parent = cards.products;
  cards.stack.emit("click", { target: heading });
  assert.equal(cards.stack.classList.contains("home-focus-products"), true);
  assert.equal(cards.products.attributes.get("aria-current"), "true");
  assert.equal(cards.redeem.attributes.get("aria-current"), "false");
  assert.equal(cards.products.focuses[0].preventScroll, true);
  cards.stack.emit("click", { target: cards.redeem });
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  assert.equal(cards.products.attributes.get("aria-current"), "false");
  assert.equal(cards.redeem.attributes.get("aria-current"), "true");
  assert.equal(cards.redeem.focuses[0].preventScroll, true);
  assert.equal(cards.stack.scrolls.length, 0);
  controller.dispose();
  for (const event of ["click", "focusin", "keydown", "scroll"])
    assert.equal(cards.stack.listeners.get(event).size, 0);
  assert.equal(page.mobileMedia.listeners.get("change").size, 0);
  assert.equal(page.document.listeners.get("visibilitychange").size, 0);
});

test("表单与商品控制通过聚焦切换视觉焦点，点击不抢走输入或链接焦点", () => {
  const page = fixture({ mobile: true });
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  const initialScrolls = cards.stack.scrolls.length;
  for (const tag of ["button", "a", "input", "select", "textarea", "label", "summary"]) {
    const control = new page.Element([], tag);
    control.parent = cards.products;
    const child = new page.Element();
    child.parent = control;
    cards.stack.emit("click", { target: child });
    assert.equal(cards.stack.classList.contains("home-focus-redeem"), true, tag);
    assert.equal(cards.products.focuses.length, 0, tag);
    assert.equal(cards.stack.scrolls.length, initialScrolls, tag);
  }
  for (const attribute of [["contenteditable", "true"], ["role", "button"]]) {
    const control = new page.Element();
    control.setAttribute(...attribute);
    control.parent = cards.products;
    cards.stack.emit("click", { target: control });
    assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  }
  cards.stack.emit("click", { target: cards.products, defaultPrevented: true });
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  const input = new page.Element([], "input");
  input.parent = cards.products;
  cards.stack.emit("focusin", { target: input });
  assert.equal(cards.stack.classList.contains("home-focus-products"), true);
  assert.equal(cards.products.focuses.length, 0);
  assert.equal(cards.stack.scrolls.length, initialScrolls);
  controller.dispose();
});

test("方向键只切换纸卡自身的焦点，输入和列表滚动键保留原生行为", () => {
  const page = fixture();
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  let prevented = 0;
  const key = (target, value, extra = {}) => cards.stack.emit("keydown", {
    target, key: value, preventDefault: () => prevented++, ...extra,
  });
  cards.stack.emit("focusin", { target: cards.products });
  key(cards.products, "ArrowRight");
  assert.equal(prevented, 1);
  assert.equal(cards.redeem.attributes.get("aria-current"), "true");
  assert.equal(cards.redeem.focuses[0].preventScroll, true);
  key(cards.redeem, "ArrowLeft");
  assert.equal(prevented, 2);
  assert.equal(cards.products.attributes.get("aria-current"), "true");
  const input = new page.Element([], "input");
  input.parent = cards.redeem;
  const list = new page.Element();
  list.parent = cards.products;
  for (const target of [input, list]) for (const value of ["ArrowLeft", "ArrowRight", "ArrowDown", " "]) key(target, value);
  for (const modifier of ["altKey", "ctrlKey", "metaKey", "shiftKey"]) key(cards.products, "ArrowRight", { [modifier]: true });
  key(cards.products, "Enter");
  assert.equal(prevented, 2);
  assert.equal(cards.products.attributes.get("aria-current"), "true");
  controller.dispose();
});

test("手机默认兑换纸卡，原生横滑同步纸卡焦点；降低动态时直接切换", () => {
  const page = fixture({ mobile: true, reduced: true });
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  assert.equal(cards.stack.scrolls[0].left, 370);
  assert.equal(cards.stack.scrolls[0].behavior, "auto");
  cards.stack.scrollLeft = 0;
  cards.stack.emit("scroll", { target: cards.stack });
  assert.equal(cards.products.attributes.get("aria-current"), "true");
  assert.equal(cards.stack.scrolls.length, 1);
  controller.show("redeem");
  assert.equal(cards.stack.scrolls.at(-1).behavior, "auto");
  controller.dispose();
});

test("手机内层商品列表纵向滚动不触发横向纸卡选择，纸卡点击仍平滑定位", () => {
  const page = fixture({ mobile: true });
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  const list = new page.Element();
  list.parent = cards.products;
  cards.stack.scrollLeft = 0;
  cards.stack.emit("scroll", { target: list });
  assert.equal(cards.redeem.attributes.get("aria-current"), "true");
  assert.equal(cards.stack.scrolls.length, 1);
  cards.stack.emit("scroll", { target: cards.stack });
  assert.equal(cards.products.attributes.get("aria-current"), "true");
  cards.stack.emit("click", { target: cards.redeem });
  assert.equal(cards.stack.scrolls.at(-1).left, 370);
  assert.equal(cards.stack.scrolls.at(-1).behavior, "smooth");
  controller.dispose();
});

test("首页重绘清理旧纸卡事件，断开的纸卡与已销毁控制器不能继续选择", () => {
  const page = fixture();
  const old = home(page);
  const controller = page.module.mountHome(page.app);
  const next = home(page);
  assert.equal(page.module.mountHome(page.app), controller);
  for (const event of ["click", "focusin", "keydown", "scroll"])
    assert.equal(old.stack.listeners.get(event).size, 0);
  old.stack.emit("click", { target: old.products });
  assert.equal(next.products.attributes.get("aria-current"), "false");
  assert.equal(controller.show("missing"), false);
  next.stack.isConnected = false;
  assert.equal(controller.show("products"), false);
  next.stack.isConnected = true;
  controller.dispose();
  assert.equal(controller.show("products"), false);
  assert.equal(controller.disposed, true);
});

test("真实首页模板移除切换按钮，纸卡与纵向商品列表可键盘聚焦", async () => {
  const homeSource = appSource.slice(appSource.indexOf("async function home() {"), appSource.indexOf("function receiptRequestContext("));
  const app = { innerHTML: "" };
  let list = [{ name: '<img src=x onerror="alert(1)">', logo: "", delivery: "content", mode: "manual" }];
  let mounts = 0;
  let refreshes = 0;
  const controls = [];
  const code = { addEventListener: (event, handler) => {
    assert.equal(event, "input");
    assert.equal(typeof handler, "function");
  } };
  const context = {
    queueLoadId: 0, receiptMotion: null, receiptViewKey: "old", currentToken: "old",
    currentBatch: {}, batchSelection: "old-card", batchRetryOnly: true,
    currentProduct: {}, currentVariant: {}, app,
    api: async (route) => { assert.equal(route, "/products"); return list; },
    tr: (zh) => zh,
    esc: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    icon: "<svg></svg>", form: (handler) => assert.equal(typeof handler, "function"),
    on: (selector, handler) => { controls.push(selector); assert.equal(typeof handler, "function"); },
    pasteCodes: async () => {}, updateCodeCount: () => {},
    $: (selector) => { assert.equal(selector, "#code"); return code; },
    window: { ExtoreMotion: { mountHome: (target) => { assert.equal(target, app); mounts++; } }, ExtoreWebMCP: { refresh: () => refreshes++ } },
    document: { querySelectorAll: () => [] },
  };
  const render = vm.runInNewContext(`${homeSource}\nhome`, context);
  await render();
  assert.doesNotMatch(app.innerHTML, /home-paper-tabs|home-show-products|home-show-redeem|<nav/);
  assert.match(app.innerHTML, /data-home-paper="products" tabindex="0" aria-label="公开商品"/);
  assert.match(app.innerHTML, /data-home-paper="redeem" tabindex="0" aria-label="卡密兑换"/);
  assert.match(app.innerHTML, /class="home-product-list" tabindex="0" role="region" aria-label="公开商品列表，可上下滚动"/);
  assert.match(app.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(app.innerHTML, /<form id="form">/);
  assert.equal(context.currentToken, "");
  assert.equal(context.currentBatch, null);
  assert.equal(context.batchSelection, "");
  assert.equal(context.batchRetryOnly, false);
  assert.equal(controls[0], "#paste-code");
  list = [];
  await render();
  assert.match(app.innerHTML, /<h1>暂无公开商品<\/h1>/);
  assert.doesNotMatch(app.innerHTML, /class="home-product-list"/);
  assert.equal(mounts, 2);
  assert.equal(refreshes, 2);
});
