const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../extore/static/customer-motion.js"), "utf8");
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
    constructor(classes = []) {
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
      this.rect = { left: 50, top: 50, right: 450, bottom: 350, width: 400, height: 300 };
      this.offsetLeft = 0;
      this.offsetWidth = 350;
      this.scrollLeft = 0;
      this.clientWidth = 386;
      this.scrolls = [];
    }
    querySelector(selector) { return this.selectors.get(selector) || null; }
    querySelectorAll(selector) { return this.selectors.get(selector) || []; }
    getBoundingClientRect() { return this.rect; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    append(node) { this.children.push(node); }
    remove() { this.isConnected = false; }
    cloneNode() { return new Element(); }
    scrollTo(options) { this.scrolls.push(options); this.scrollLeft = options.left; }
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
  products.offsetLeft = 18;
  const redeem = new page.Element();
  redeem.dataset.homePaper = "redeem";
  redeem.offsetLeft = 388;
  stack.selectors.set("[data-home-paper]", [products, redeem]);
  page.app.selectors.set(".home-paper-stack", stack);
  const productButton = new page.Element();
  const redeemButton = new page.Element();
  page.app.selectors.set("#home-show-products", productButton);
  page.app.selectors.set("#home-show-redeem", redeemButton);
  return { stack, products, redeem, productButton, redeemButton };
}

test("桌面纸卡保持初始双卡，点击或表单聚焦只更新焦点与导航状态", () => {
  const page = fixture();
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  assert.equal(page.module.mountHome(page.app), controller);
  assert.equal(cards.stack.classList.contains("home-focus-products"), false);
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), false);
  controller.show("products");
  assert.equal(cards.productButton.attributes.get("aria-current"), "true");
  cards.stack.emit("focusin", { target: { closest: () => cards.redeem } });
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  assert.equal(cards.redeemButton.attributes.get("aria-current"), "true");
  assert.equal(cards.stack.scrolls.length, 0);
  controller.dispose();
  assert.equal(cards.stack.listeners.get("click").size, 0);
});

test("手机默认兑换纸卡，原生滑动同步导航；降低动态时直接切换", () => {
  const page = fixture({ mobile: true, reduced: true });
  const cards = home(page);
  const controller = page.module.mountHome(page.app);
  assert.equal(cards.stack.classList.contains("home-focus-redeem"), true);
  assert.equal(cards.stack.scrolls[0].left, 370);
  assert.equal(cards.stack.scrolls[0].behavior, "auto");
  cards.stack.scrollLeft = 0;
  cards.stack.emit("scroll");
  assert.equal(cards.productButton.attributes.get("aria-current"), "true");
  controller.show("redeem");
  assert.equal(cards.stack.scrolls.at(-1).behavior, "auto");
  controller.dispose();
});
