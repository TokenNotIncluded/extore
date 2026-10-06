"use strict";
const $ = (s) => document.querySelector(s),
  app = $("#app");
const preferences = window.ExtorePreferences;
let lang = preferences.resolved.language,
  currentToken = "",
  currentProduct = null,
  timer = null,
  role = null,
  permissions = [],
  managedProductId = null,
  managementName = "",
  managementExpires = null,
  tab = "products",
  products = [],
  cardProductId = "",
  queueProductId = "",
  queueProduct = null,
  queueLoadId = 0;
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const tr = (cn, en) => (lang === "en" ? en : cn);
const localized = (value) =>
  typeof value === "object"
    ? value?.[lang] || value?.["zh-CN"] || Object.values(value || {})[0] || ""
    : value;
const md = (s) =>
  DOMPurify.sanitize(marked.parse(String(s || "")), {
    FORBID_TAGS: ["img", "style", "iframe", "form", "input"],
    FORBID_ATTR: ["style", "id"],
  });
const icon =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m4 7 8-4 8 4v10l-8 4-8-4zM4 7l8 4 8-4M12 11v10"/></svg>';
const states = {
  queued: "排队中",
  processing: "处理中",
  succeeded: "已完成",
  failed: "未完成",
  destroyed: "已销毁",
};
const stateEn = {
  queued: "Queued",
  processing: "Processing",
  succeeded: "Completed",
  failed: "Failed",
  destroyed: "Destroyed",
};
const status = (j) =>
  `<span class="status ${esc(j.state)}">${esc(lang === "en" ? stateEn[j.state] : states[j.state])}</span>`;
function toast(s) {
  $("#toast").textContent = s;
  $("#toast").hidden = false;
  setTimeout(() => ($("#toast").hidden = true), 3500);
}
async function api(path, body, method, options = {}) {
  const response = await fetch("/api" + path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: options.signal,
  });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("服务器返回异常，请稍后重试");
  }
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "输入格式有误，请检查表单",
    );
  return data;
}
function on(id, handler) {
  const node = $(id);
  if (node) node.addEventListener("click", () => perform(handler, node));
}
async function perform(handler, node) {
  if (node) node.disabled = true;
  try {
    await handler();
  } catch (e) {
    const error = $("#error");
    if (error) error.textContent = e.message;
    else toast(e.message);
  } finally {
    if (node?.isConnected) node.disabled = false;
  }
}
function form(handler) {
  $("#form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    perform(handler, $("#form button[type=submit]"));
  });
}
function stopPoll() {
  clearTimeout(timer);
  timer = null;
}
function navigate(path, nextTab) {
  queueLoadId++;
  if (nextTab) tab = nextTab;
  stopPoll();
  history.pushState({}, "", path);
  return start();
}
function field(id, label, value = "", type = "text") {
  return `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="${type}" value="${esc(value)}"></div>`;
}
function select(id, label, options, value) {
  return `<div class="field"><label for="${id}">${label}</label><select id="${id}">${options.map(([v, l]) => `<option value="${v}" ${value === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>`;
}
function textarea(id, label, value = "") {
  return `<div class="field"><label for="${id}">${label}</label><textarea id="${id}">${esc(value)}</textarea></div>`;
}
let clicks = [];
$("#brand").addEventListener("click", () => {
  const now = Date.now();
  clicks = clicks.filter((t) => now - t < 2500);
  clicks.push(now);
  if (clicks.length >= 5) {
    clicks = [];
    navigate("/admin");
  } else if (location.pathname !== "/") navigate("/");
});
function syncPreferenceControls() {
  $("#theme").value = preferences.settings.theme;
  $("#language").value = preferences.settings.language;
  $("#theme").setAttribute("aria-label", tr("主题", "Theme"));
  $("#language").setAttribute("aria-label", tr("语言", "Language"));
  $("#theme option[value=auto]").textContent = tr("主题：自动", "Theme: Auto");
  $("#theme option[value=light]").textContent = tr("明亮", "Light");
  $("#theme option[value=dark]").textContent = tr("深色", "Dark");
  $("#language option[value=auto]").textContent = tr(
    "语言：自动",
    "Language: Auto",
  );
  $("#header-context").textContent = tr("兑换与领取", "Redeem & collect");
}
syncPreferenceControls();
preferences.subscribe(({ resolved }) => {
  const changed = lang !== resolved.language;
  lang = resolved.language;
  syncPreferenceControls();
  if (changed) start();
});
$("#language").addEventListener("change", () =>
  preferences.setLanguage($("#language").value),
);
$("#theme").addEventListener("change", () =>
  preferences.setTheme($("#theme").value),
);
window.addEventListener("popstate", start);

async function home() {
  queueLoadId++;
  currentToken = "";
  currentProduct = null;
  const list = await api("/products");
  app.innerHTML = `<section class="intro"><div class="intro-copy"><h1>${list.length ? tr("公开商品", "Public products") : tr("暂无公开商品", "No public products")}</h1><div class="paper-divider" aria-hidden="true"></div></div><section class="exchange"><h2>${tr("开始兑换", "Redeem your code")}</h2><p>${tr("请使用购买商品时获得的卡密。", "Use the code you received with your purchase.")}</p><form id="form"><div class="field"><label for="code">${tr("兑换卡密", "Redemption code")}</label><input class="code" id="code" placeholder="XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX" required autocomplete="off" spellcheck="false" maxlength="128"></div><button type="submit" class="full">${tr("验证并继续", "Verify and continue")} →</button><div id="error" class="error" role="alert"></div></form><div class="footnote">${tr("已有领取链接？直接打开即可查看处理进度。", "Already have a receipt link? Open it to check progress.")}</div></section></section>${list.length ? `<section class="catalog"><div class="section-head"><div><h2>${tr("可兑换商品", "Available products")}</h2><p>${tr("选择商品了解详情，兑换仍需有效卡密。", "Browse the details. A valid code is required to redeem.")}</p></div></div><div class="product-list">${list.map((p, i) => `<article class="product-row">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${esc(p.name)}</h3><p>${tr(p.delivery === "service" ? "服务兑换" : "商品领取", p.delivery === "service" ? "Service" : "Digital delivery")} · ${tr(p.mode === "manual" ? "队列处理" : "自动处理", p.mode === "manual" ? "Queue processing" : "Automatic")}</p></div><button class="secondary" data-product="${i}">${tr("查看详情", "Details")}</button></article>`).join("")}</div></section>` : ""}`;
  form(() => exchangeCode($("#code").value));
  window.ExtoreWebMCP?.refresh();
  document
    .querySelectorAll("[data-product]")
    .forEach((b) =>
      b.addEventListener("click", () => publicDetail(list[+b.dataset.product])),
    );
}
function receiptRequestContext(options = {}) {
  const token = currentToken;
  const pathname = location.pathname;
  const hash = location.hash;
  return {
    token,
    active: () =>
      currentToken === token &&
      location.pathname === pathname &&
      location.hash === hash &&
      !options.signal?.aborted,
  };
}
async function exchangeCode(code, options = {}) {
  const context = receiptRequestContext(options);
  const data = await api("/exchange", { code }, "POST", options);
  if (!context.active()) return data;
  currentToken = data.token;
  currentProduct = data.product;
  history.pushState({}, "", "/receipt#" + currentToken);
  data.job ? renderReceipt(data.job) : redemptionForm();
  return data;
}
async function submitRedemption(params, options = {}) {
  const context = receiptRequestContext(options);
  const result = await api(
    "/redeem",
    { token: context.token, params },
    "POST",
    options,
  );
  if (context.active()) renderReceipt(result);
  return result;
}
async function readReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const result = await api(
    "/receipt",
    { token: context.token },
    "POST",
    options,
  );
  if (context.active()) {
    currentProduct = result.product;
    result.job ? renderReceipt(result.job) : redemptionForm();
  }
  return result;
}
async function revealReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const viewPolicy = currentProduct?.view_policy;
  const result = await api(
    "/receipt/reveal",
    { token: context.token },
    "POST",
    options,
  );
  if (context.active()) {
    const content = $("#content");
    if (content) {
      const output = result.output || { content: result.content };
      const fields = currentProduct?.outputs || [];
      content.innerHTML = Object.entries(output)
        .map(([key, value]) => {
          const definition = fields.find((field) => field.key === key);
          return `<div class="delivery-field"><h3>${esc(localized(definition?.label) || key)}</h3><pre class="result">${esc(value)}</pre></div>`;
        })
        .join("");
    }
    if (viewPolicy === "once") $("#reveal")?.remove();
  }
  return result;
}
async function destroyReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const result = await api(
    "/receipt/destroy",
    { token: context.token },
    "POST",
    options,
  );
  if (context.active()) {
    const content = $("#content");
    if (content) content.innerHTML = "";
    $("#reveal")?.remove();
    $("#destroy")?.remove();
    try {
      await readReceipt(options);
    } catch {
      if (context.active())
        toast(
          tr(
            "交付已销毁，状态刷新失败，请刷新页面。",
            "Delivery destroyed. Refresh the page to reload its status.",
          ),
        );
    }
  }
  return result;
}
function publicDetail(p) {
  app.innerHTML = `<div class="narrow"><button class="secondary" id="back">← ${tr("返回", "Back")}</button><div class="panel"><h1>${esc(p.name)}</h1>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="${esc(p.name)}">` : ""}<div class="markdown">${md(p.description)}</div><button id="return" class="full">${tr("输入卡密兑换", "Enter a code to redeem")}</button></div></div>`;
  on("#back", home);
  on("#return", home);
}
function redemptionForm() {
  stopPoll();
  window.ExtoreWebMCP?.refresh();
  const p = currentProduct;
  app.innerHTML = `<div class="narrow"><h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("填写信息", "Your details")}</div><div class="step">${tr("领取商品", "Collect")}</div></div>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="">` : ""}<div class="markdown">${md(p.description)}</div><div class="panel"><h2>${tr("确认兑换信息", "Confirm your details")}</h2><form id="form">${p.parameters.map((f) => `<div class="field"><label for="param-${f.key}">${esc(localized(f.label))}${f.required ? " *" : ""}</label>${f.type === "textarea" ? `<textarea id="param-${f.key}" ${f.required ? "required" : ""} maxlength="10000"></textarea>` : `<input id="param-${f.key}" type="${f.type}" ${f.type === "number" ? 'step="any"' : ""} ${f.required ? "required" : ""} maxlength="10000">`}</div>${localized(f.description) ? `<details ${f.collapsed ? "" : "open"}><summary>${tr("填写说明", "Instructions")}</summary><div class="markdown">${md(localized(f.description))}</div></details>` : ""}`).join("")}<button type="submit" class="full">${tr("确认兑换", "Confirm redemption")}</button><p class="caption">${tr("提交后会创建兑换任务，请保存领取链接。", "Save your receipt link after submitting.")}</p><div id="error" class="error" role="alert"></div></form></div></div>`;
  form(async () => {
    const params = Object.fromEntries(
      p.parameters.map((f) => [f.key, $("#param-" + f.key).value]),
    );
    await submitRedemption(params);
  });
}
function renderReceipt(j) {
  stopPoll();
  window.ExtoreWebMCP?.refresh();
  const p = currentProduct;
  app.innerHTML = `<div class="narrow"><h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("信息已提交", "Details submitted")}</div><div class="step ${j.state === "succeeded" ? "active" : ""}">${tr("领取商品", "Collect")}</div></div><section class="panel"><div class="section-head"><h2>${tr("兑换进度", "Redemption progress")}</h2>${status(j)}</div><p>${esc(j.message || tr(j.state === "queued" ? "已进入处理队列。" : "我们会在这里更新处理结果。", j.state === "queued" ? "Your task is in the queue." : "Updates will appear here."))}</p>${j.state === "queued" ? `<p class="caption">${tr("前面还有 " + j.queue_ahead + " 个任务", "There are " + j.queue_ahead + " tasks ahead.")}</p>` : ""}${["processing", "queued"].includes(j.state) ? `<div class="progress" role="progressbar" aria-label="处理进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${j.progress}"><span></span></div><p class="caption">${j.progress}%</p>` : ""}<div id="content"></div><div class="actions">${j.state === "succeeded" && j.delivery === "content" ? `<button id="reveal">${tr(j.view_policy === "once" ? "领取内容（仅一次）" : "查看交付内容", j.view_policy === "once" ? "Reveal once" : "View your goods")}</button>` : ""}${j.can_retry ? `<button id="retry">${tr("重新填写并重试", "Update details and retry")}</button>` : ""}${j.state === "succeeded" ? `<button id="destroy" class="danger">${tr("立即销毁", "Destroy now")}</button>` : ""}</div><div id="error" class="error" role="alert"></div>${j.state === "destroyed" ? `<p class="caption">${tr("内容已永久删除，此链接无法再领取。", "The content has been deleted. This link can no longer reveal it.")}</p>` : ""}</section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(location.origin + "/receipt#" + currentToken)}</p><button id="copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("有效期 30 天。链接是领取凭证，请勿转发给他人。", "Valid for 30 days. Anyone with this link can access the receipt.")}</p></div></div>`;
  // No inline style under CSP: assign via CSSOM.
  const bar = $(".progress span");
  if (bar) bar.style.transform = "scaleX(" + j.progress / 100 + ")";
  on("#reveal", async () => {
    if (
      j.view_policy === "once" &&
      !confirm(
        tr(
          "内容只能打开一次，打开后请自行保存。继续领取？",
          "This content can only be revealed once. Save it after opening. Continue?",
        ),
      )
    )
      return;
    await revealReceipt();
  });
  on("#destroy", async () => {
    if (
      !confirm(
        tr(
          "永久删除交付内容并关闭领取。继续销毁？",
          "Permanently delete the delivery and disable access?",
        ),
      )
    )
      return;
    await destroyReceipt();
  });
  on("#retry", redemptionForm);
  on("#copy", async () => {
    await navigator.clipboard.writeText(
      location.origin + "/receipt#" + currentToken,
    );
    toast(tr("链接已复制", "Link copied"));
  });
  if (["queued", "processing"].includes(j.state)) {
    const context = receiptRequestContext();
    timer = setTimeout(async () => {
      if (!context.active()) return;
      try {
        await readReceipt();
      } catch (e) {
        if (context.active()) toast(e.message);
      }
    }, 2500);
  }
}

const decode = (s) =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) =>
    c.charCodeAt(0),
  );
const encode = (b) =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
async function passkey(register = false) {
  const options = await api(
    "/auth/" + (register ? "register" : "login") + "/options",
    {},
  );
  options.challenge = decode(options.challenge);
  if (register) {
    options.user.id = decode(options.user.id);
    options.excludeCredentials?.forEach((x) => (x.id = decode(x.id)));
  } else options.allowCredentials?.forEach((x) => (x.id = decode(x.id)));
  const c = register
    ? await navigator.credentials.create({ publicKey: options })
    : await navigator.credentials.get({ publicKey: options });
  const response = { clientDataJSON: encode(c.response.clientDataJSON) };
  if (register) {
    response.attestationObject = encode(c.response.attestationObject);
    response.transports = c.response.getTransports?.() || [];
  } else {
    response.authenticatorData = encode(c.response.authenticatorData);
    response.signature = encode(c.response.signature);
    response.userHandle = c.response.userHandle
      ? encode(c.response.userHandle)
      : null;
  }
  await api("/auth/" + (register ? "register" : "login") + "/verify", {
    credential: {
      id: c.id,
      rawId: encode(c.rawId),
      type: c.type,
      response,
      clientExtensionResults: c.getClientExtensionResults(),
    },
    name: $("#key-name")?.value || "我的 Passkey",
  });
}
async function admin() {
  stopPoll();
  const auth = await api("/auth/status");
  acceptAuth(auth);
  window.ExtoreWebMCP?.refresh();
  if (!auth.configured) {
    app.innerHTML =
      '<div class="narrow"><h1>先初始化商家账号</h1><div class="panel"><p>在服务器执行以下命令，设置首次登录密码。</p><pre class="result">uv run python -m extore.cli init</pre><p class="muted">初始化后刷新页面。注册第一个 Passkey 后，密码登录会立即禁用。</p></div></div>';
    return;
  }
  if (role === "bootstrap") {
    app.innerHTML = `<div class="narrow"><h1>注册你的 Passkey</h1><p>用设备指纹、面容或安全密钥登录后台。注册成功后，密码登录会立即禁用。</p><div class="panel">${field("key-name", "设备名称", "我的 Passkey")}<button id="register" class="full">注册 Passkey</button><div id="error" class="error" role="alert"></div></div></div>`;
    on("#register", async () => {
      await passkey(true);
      await admin();
    });
    return;
  }
  if (role !== "admin") {
    app.innerHTML = `<div class="narrow"><h1>商家后台</h1><p>${auth.password_enabled ? "首次登录后，请注册 Passkey。" : "使用你的 Passkey 登录。"}</p><div class="panel">${auth.password_enabled ? `<form id="form">${field("password", "首次登录密码", "", "password")}<button class="full" type="submit">登录并注册 Passkey</button></form>` : '<button id="login" class="full">使用 Passkey 登录</button>'}<div id="error" class="error" role="alert"></div></div></div>`;
    form(async () => {
      await api("/auth/password", { password: $("#password").value });
      await admin();
    });
    on("#login", async () => {
      await passkey();
      await admin();
    });
    return;
  }
  products = await api("/admin/products");
  shell();
  await renderTab();
}
const permissionLabels = {
  "queue.view": "查看商品队列",
  "queue.process": "领取任务、更新进度与交付",
  "queue.retry": "核实后允许失败任务重试",
  "product.edit": "修改商品介绍、图片与填写参数",
  "fulfillment.configure": "配置自动发货、签名密钥与重试规则",
  "cards.manage": "发行、查看与撤销卡密",
  "events.manage": "查看事件与重新投递",
  "links.delegate": "创建与撤销下级管理链接",
};
function acceptAuth(auth) {
  role = auth.role;
  permissions = auth.permissions || [];
  managedProductId = auth.product_id || null;
  managementName = auth.link_name || "";
  managementExpires = auth.link_expires || null;
}
const permitted = (permission) =>
  role === "admin" || permissions.includes(permission);
function managementTabs() {
  const items = [
    ["products", "商品", "product.edit"],
    ["jobs", "处理队列", "queue.view"],
    ["cards", "卡密", "cards.manage"],
    ["staff", "管理链接", "links.delegate"],
    ["events", "事件记录", "events.manage"],
  ].filter(([, , permission]) => permitted(permission));
  if (role === "admin") items.push(["security", "Passkey"]);
  return items;
}
function shell() {
  const items = managementTabs();
  if (!items.some(([key]) => key === tab)) tab = items[0]?.[0] || "";
  app.innerHTML = `<div class="admin-top"><div><h1>${role === "staff" ? "商品管理" : "商家后台"}</h1><p class="muted">${role === "staff" ? esc(managementName) + " · 只管理被授权的商品。" : "管理商品、兑换任务与交付。"}</p></div><button id="logout" class="secondary">退出登录</button></div><nav class="tabs" aria-label="管理导航">${items.map(([v, label]) => `<button data-tab="${v}" class="${tab === v ? "active" : ""}">${label}</button>`).join("")}</nav><div id="workspace"></div>`;
  on("#logout", async () => {
    await api("/auth/logout", {});
    navigate("/admin");
  });
  document.querySelectorAll("[data-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      tab = b.dataset.tab;
      shell();
      perform(renderTab);
    }),
  );
}
async function renderTab() {
  queueLoadId++;
  window.ExtoreWebMCP?.refresh();
  if (tab === "products") {
    if (role === "staff") products = [await api("/manage/product")];
    await renderProducts();
  }
  if (tab === "jobs") await renderJobs();
  if (tab === "cards") await renderCards();
  if (tab === "staff") await renderStaff();
  if (tab === "events") await renderEvents();
  if (tab === "security") await renderSecurity();
}
function productOptions() {
  return products
    .map((p) => `<option value="${p.id}">${esc(p.name)}</option>`)
    .join("");
}
function productUIContext() {
  const loadId = queueLoadId;
  const pathname = location.pathname;
  return {
    api,
    workspace: $("#workspace"),
    products,
    role,
    canConfigure: permitted("fulfillment.configure"),
    lang,
    isCurrent: () =>
      loadId === queueLoadId &&
      location.pathname === pathname &&
      tab === "products",
    onSaved: (updated) => {
      products = updated;
    },
    notify: toast,
    refreshTools: () => window.ExtoreWebMCP?.refresh(),
  };
}
async function renderProducts() {
  return window.ExtoreProducts.render(productUIContext());
}
async function editProduct(p) {
  return window.ExtoreProducts.edit(productUIContext(), p);
}
async function renderJobs(filter = "", requestedProductId = queueProductId) {
  const loadId = ++queueLoadId;
  const pathname = location.pathname;
  const active = () =>
    loadId === queueLoadId && location.pathname === pathname && tab === "jobs";
  const available = await api("/manage/products");
  if (!active()) return;
  if (!available.length) {
    queueProductId = "";
    queueProduct = null;
    $("#workspace").innerHTML =
      '<div class="empty">暂无商品队列。请先创建商品。</div>';
    window.ExtoreWebMCP?.refresh();
    return;
  }
  const selectedProduct =
    available.find((p) => p.id === requestedProductId) || available[0];
  const productId = selectedProduct.id;
  queueProductId = productId;
  queueProduct = selectedProduct;
  const query = new URLSearchParams({ product_id: productId });
  if (filter) query.set("state", filter);
  const rows = await api("/manage/jobs?" + query);
  if (!active()) return;
  const manual = selectedProduct.mode === "manual";
  const canProcess = manual && permitted("queue.process");
  $("#workspace").innerHTML = `
    <div class="field queue-picker"><label for="queue-product">选择商品队列</label><select id="queue-product" ${role === "staff" ? "disabled" : ""}>${available.map((p) => `<option value="${esc(p.id)}" ${p.id === productId ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>
    <div class="queue-heading"><h2>${esc(selectedProduct.name)} · 处理队列</h2><p class="caption">${manual ? "本队列只处理这个商品。先领取任务，再更新进度或提交结果。" : "本商品由程序自动处理，这里查看进度与处理记录。"}</p></div>
    <div class="toolbar"><select id="job-state" aria-label="任务状态"><option value="">全部状态</option>${Object.entries(
      states,
    )
      .map(
        ([k, v]) =>
          `<option value="${k}" ${filter === k ? "selected" : ""}>${v}</option>`,
      )
      .join(
        "",
      )}</select><button id="refresh" class="secondary">刷新</button>${canProcess ? '<button id="claim">领取选中任务</button><button id="progress-update" class="secondary">更新进度</button><button id="complete" class="secondary">批量完成</button><button id="fail" class="secondary">标记失败</button>' : ""}${role === "admin" ? '<button id="release" class="secondary">核实后允许重试</button>' : ""}</div>
    ${manual ? '<p class="caption">批量操作只作用于当前商品，全部成功才提交。批量完成会给所选任务相同的交付内容。</p>' : ""}
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th><input id="all" type="checkbox" aria-label="选择当前商品的全部任务"></th><th>任务</th><th>用户参数</th><th>状态 / 进度</th><th>尝试</th></tr></thead><tbody>${rows.map((j) => `<tr><td><input type="checkbox" name="job" value="${esc(j.id)}" aria-label="选择 ${esc(j.id)}"></td><td class="mono">${esc(j.id)}</td><td><pre>${esc(JSON.stringify(j.params, null, 2))}</pre></td><td>${status(j)}<p>${j.progress}% · ${esc(j.message)}</p></td><td>${j.attempt}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">这个商品暂无符合条件的任务。</div>'}
    <div id="batch-form"></div><div id="error" class="error" role="alert"></div>`;
  window.ExtoreWebMCP?.refresh();
  $("#queue-product").addEventListener("change", () =>
    perform(() => renderJobs("", $("#queue-product").value)),
  );
  $("#job-state").addEventListener("change", () =>
    perform(() => renderJobs($("#job-state").value, productId)),
  );
  on("#refresh", () => renderJobs(filter, productId));
  $("#all")?.addEventListener("change", () =>
    document
      .querySelectorAll("[name=job]")
      .forEach((c) => (c.checked = $("#all").checked)),
  );
  const ids = () => {
    const values = [...document.querySelectorAll("[name=job]:checked")].map(
      (c) => c.value,
    );
    if (!values.length) throw new Error("请先选择当前商品的任务");
    return values;
  };
  const executeBatch = (body) =>
    api("/manage/batch", { product_id: productId, ...body });
  on("#claim", async () => {
    await executeBatch({ ids: ids(), action: "claim" });
    await renderJobs(filter, productId);
  });
  on("#release", async () => {
    if (!confirm("确认已核实外部平台没有交付？允许重试可能再次调用发货程序。"))
      return;
    await executeBatch({ ids: ids(), action: "retry" });
    await renderJobs(filter, productId);
  });
  function finish(action) {
    const selected = ids();
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>${action === "succeed" ? "批量完成" : "标记失败"} · ${esc(selectedProduct.name)} · ${selected.length} 个任务</h2>${textarea("batch-message", "处理说明")}${action === "succeed" ? (selectedProduct.outputs || []).map((output, i) => `<div class="field"><label for="batch-output-${i}">${esc(localized(output.label))}${output.required ? " *" : ""}</label>${output.type === "textarea" ? `<textarea id="batch-output-${i}" maxlength="100000" ${output.required ? "required" : ""}></textarea>` : `<input id="batch-output-${i}" type="${output.type}" ${output.type === "number" ? 'step="any"' : ""} maxlength="100000" ${output.required ? "required" : ""}>`}</div>${localized(output.description) ? `<details ${output.collapsed ? "" : "open"}><summary>交付说明</summary><div class="markdown">${md(localized(output.description))}</div></details>` : ""}`).join("") || '<p class="caption">此商品只交付服务状态，无需填写内容。</p>' : '<div class="checks"><label><input id="batch-retry" type="checkbox">已确认未交付，允许顾客重试</label></div>'}<div class="actions"><button id="batch-submit">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", () => ($("#batch-form").innerHTML = ""));
    on("#batch-submit", async () => {
      const output = {};
      if (action === "succeed") {
        for (const [i, definition] of (
          selectedProduct.outputs || []
        ).entries()) {
          const input = $("#batch-output-" + i);
          if (!input.reportValidity()) return;
          output[definition.key] = input.value;
        }
      }
      await executeBatch({
        ids: selected,
        action,
        message: $("#batch-message").value,
        output: action === "succeed" ? output : undefined,
        retryable: $("#batch-retry")?.checked || false,
      });
      await renderJobs(filter, productId);
    });
  }
  on("#progress-update", () => {
    const selected = ids();
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>更新进度 · ${esc(selectedProduct.name)}</h2>${field("batch-progress", "进度（0–99）", 30, "number")}${field("batch-message", "处理说明")}<button id="batch-submit" class="full">提交进度</button></div>`;
    on("#batch-submit", async () => {
      await executeBatch({
        ids: selected,
        action: "progress",
        progress: +$("#batch-progress").value,
        message: $("#batch-message").value,
      });
      await renderJobs(filter, productId);
    });
  });
  on("#complete", () => finish("succeed"));
  on("#fail", () => finish("fail"));
}
async function renderCards() {
  const loadId = queueLoadId;
  const pathname = location.pathname;
  if (role === "staff") products = await api("/manage/products");
  if (
    loadId !== queueLoadId ||
    pathname !== location.pathname ||
    tab !== "cards"
  )
    return;
  return window.ExtoreCards.render({
    api,
    workspace: $("#workspace"),
    products,
    role,
    productId: role === "staff" ? managedProductId : cardProductId,
    isCurrent: () =>
      loadId === queueLoadId &&
      pathname === location.pathname &&
      tab === "cards",
    onContextChange: (productId) => {
      cardProductId = productId;
      window.ExtoreWebMCP?.refresh();
    },
  });
}
async function renderStaff() {
  const endpoint = role === "staff" ? "/manage/links" : "/admin/staff";
  if (role === "staff") products = await api("/manage/products");
  const links = await api(endpoint);
  const availablePermissions = Object.keys(permissionLabels).filter(permitted);
  const remainingDays = managementExpires
    ? Math.max(0, (managementExpires * 1000 - Date.now()) / 86400000)
    : 90;
  const defaultDays =
    role === "staff" ? Math.min(7, Math.floor(remainingDays * 100) / 100) : 7;
  $("#workspace").innerHTML =
    `<h2>商品管理链接</h2><p class="caption">一条链接只授权一个商品。可以分别授予队列、商品配置、卡密等权限；店长可获得该商品的完整管理权限。链接是登录凭证，请私下交给管理者。</p>${role === "staff" ? '<p class="caption">你只能创建权限比自己更小的下级链接，有效期也不能超过自己的链接。</p>' : ""}<form id="form"><div class="grid">${field("staff-name", "管理者或链接名称")}<div class="field"><label for="staff-product">授权商品</label><select id="staff-product" ${role === "staff" ? "disabled" : ""}>${productOptions()}</select></div>${field("staff-days", "有效天数", defaultDays, "number")}</div><fieldset class="permission-fields"><legend>权限范围</legend><div class="permission-presets"><button type="button" id="preset-view" class="secondary">只看队列</button><button type="button" id="preset-process" class="secondary">处理任务</button>${role === "admin" ? '<button type="button" id="preset-manager" class="secondary">店长：完全管理商品</button>' : ""}</div><div class="permission-grid">${availablePermissions.map((key) => `<label><input type="checkbox" name="link-permission" value="${key}" ${["queue.view", "queue.process"].includes(key) ? "checked" : ""}>${permissionLabels[key]}</label>`).join("")}</div><p class="caption">“创建下级管理链接”允许继续委派。下级必须少至少一项权限，不能扩大商品范围或有效期。撤销上级链接会同时撤销全部下级。</p></fieldset><button type="submit" class="full" ${products.length ? "" : "disabled"}>创建管理链接</button><div id="error" class="error" role="alert"></div></form><div id="staff-link"></div><div class="form-divider table-wrap"><table><thead><tr><th>管理链接</th><th>商品</th><th>权限</th><th>有效期</th><th></th></tr></thead><tbody>${links.map((link) => `<tr><td>${esc(link.name)}${link.parent_id ? '<div class="caption">下级链接</div>' : ""}</td><td>${esc(products.find((p) => p.id === link.product_id)?.name || link.product_id)}</td><td>${(link.permissions || []).map((key) => esc(permissionLabels[key] || key)).join("<br>")}</td><td>${link.revoked ? "已撤销" : new Date(link.expires * 1000).toLocaleString()}</td><td>${!link.revoked ? `<button data-revoke="${esc(link.id)}" class="danger">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  $("#staff-days").step = "0.01";
  $("#staff-days").min = "0.01";
  $("#staff-days").max = String(role === "staff" ? remainingDays : 90);
  const choosePermissions = (selected) =>
    document
      .querySelectorAll('[name="link-permission"]')
      .forEach((node) => (node.checked = selected.includes(node.value)));
  on("#preset-view", () => choosePermissions(["queue.view"]));
  on("#preset-process", () =>
    choosePermissions(["queue.view", "queue.process"]),
  );
  on("#preset-manager", () => choosePermissions(availablePermissions));
  document.querySelectorAll('[name="link-permission"]').forEach((node) =>
    node.addEventListener("change", () => {
      const view = $('[name="link-permission"][value="queue.view"]');
      const edit = $('[name="link-permission"][value="product.edit"]');
      const fulfillment = $(
        '[name="link-permission"][value="fulfillment.configure"]',
      );
      if (node.value === "fulfillment.configure" && node.checked && edit)
        edit.checked = true;
      if (node.value === "product.edit" && !node.checked && fulfillment)
        fulfillment.checked = false;
      if (
        node.checked &&
        ["queue.process", "queue.retry"].includes(node.value) &&
        view
      )
        view.checked = true;
      if (node.value === "queue.view" && !node.checked)
        document
          .querySelectorAll(
            '[name="link-permission"][value="queue.process"],[name="link-permission"][value="queue.retry"]',
          )
          .forEach((item) => (item.checked = false));
    }),
  );
  form(async () => {
    const selected = [
      ...document.querySelectorAll('[name="link-permission"]:checked'),
    ].map((node) => node.value);
    if (!selected.length) throw new Error("请至少选择一项权限");
    if (role === "staff" && selected.length === permissions.length)
      throw new Error("下级链接必须比你的权限更小，请至少取消一项权限");
    const result = await api(endpoint, {
      name: $("#staff-name").value,
      product_id: $("#staff-product").value,
      days: +$("#staff-days").value,
      permissions: selected,
    });
    await renderStaff();
    $("#staff-link").innerHTML =
      `<pre class="result">${esc(result.url)}</pre><p class="caption">链接只显示一次。请现在保存并私下交付。</p>`;
  });
  document.querySelectorAll("[data-revoke]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        if (!confirm("撤销此管理链接和全部下级链接，并把未完成任务放回队列？"))
          return;
        await api(endpoint + "/" + b.dataset.revoke + "/revoke", {});
        await renderStaff();
      }),
    ),
  );
}
async function renderEvents() {
  const endpoint = role === "staff" ? "/manage/events" : "/admin/events";
  const rows = await api(endpoint);
  $("#workspace").innerHTML =
    `<div class="section-head"><h2>事件记录</h2><button id="refresh" class="secondary">刷新</button></div><p class="caption">投递失败最多自动重试 8 次。外部平台须按事件 ID 去重。</p><div class="table-wrap"><table><thead><tr><th>事件</th><th>时间</th><th>Webhook</th><th>尝试</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.type)}<div class="mono muted">${r.id}</div></td><td>${new Date(r.created * 1000).toLocaleString()}</td><td>${esc(r.webhook_state || "未配置")}<div class="caption">${esc(r.error)}</div></td><td>${r.attempts ?? "—"}</td><td>${r.webhook_state === "dead" ? `<button data-event="${r.id}" class="secondary">重新投递</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  on("#refresh", renderEvents);
  document.querySelectorAll("[data-event]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        await api(endpoint + "/" + b.dataset.event + "/retry", {});
        await renderEvents();
      }),
    ),
  );
}
async function renderSecurity() {
  const keys = await api("/auth/passkeys");
  $("#workspace").innerHTML =
    `<h2>Passkey</h2><p class="caption">可以添加多个设备。密码登录已禁用；丢失全部设备时须通过 SSH 执行恢复命令。</p><div class="panel">${field("key-name", "新设备名称", "备用 Passkey")}<button id="add-key" class="full">添加 Passkey</button><div id="error" class="error" role="alert"></div></div><div class="product-list">${keys.map((k) => `<div class="product-row"><div class="product-info"><h3>${esc(k.name)}</h3><p>${new Date(k.created * 1000).toLocaleString()}</p></div>${keys.length > 1 ? `<button data-delete-key="${k.id}" class="danger">移除</button>` : ""}</div>`).join("")}</div><pre class="result">uv run python -m extore.cli reset-auth</pre>`;
  on("#add-key", async () => {
    await passkey(true);
    await renderSecurity();
  });
  document.querySelectorAll("[data-delete-key]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        if (!confirm("移除此 Passkey？")) return;
        await api("/auth/passkeys/" + b.dataset.deleteKey, null, "DELETE");
        await renderSecurity();
      }),
    ),
  );
}
async function staff() {
  stopPoll();
  if (location.hash) {
    const value = location.hash.slice(1);
    history.replaceState({}, "", "/staff");
    await api("/staff/login", { token: value });
  }
  const a = await api("/auth/status");
  if (a.role !== "staff") {
    app.innerHTML =
      '<div class="narrow"><h1>商品管理链接已失效</h1><p>请通过有效的商品管理链接进入。</p></div>';
    return;
  }
  acceptAuth(a);
  window.ExtoreWebMCP?.refresh();
  shell();
  await renderTab();
}
async function start() {
  queueLoadId++;
  stopPoll();
  app.innerHTML =
    '<div class="loading">' + tr("正在加载…", "Loading…") + "</div>";
  try {
    acceptAuth(await api("/auth/status"));
    if (location.pathname === "/admin") await admin();
    else if (location.pathname === "/staff") await staff();
    else if (location.pathname === "/receipt") {
      currentProduct = null;
      currentToken = location.hash.slice(1);
      if (!currentToken)
        throw new Error(
          tr(
            "领取链接缺少凭证，请重新输入卡密。",
            "This receipt link is missing its token. Enter your code again.",
          ),
        );
      await readReceipt();
    } else await home();
  } catch (e) {
    app.innerHTML = `<div class="narrow"><h1>${tr("暂时无法打开", "Unable to open")}</h1><p>${esc(e.message)}</p><button id="back" class="full">${tr("返回兑换页", "Back to redemption")}</button></div>`;
    on("#back", () => navigate("/"));
  } finally {
    window.ExtoreWebMCP?.refresh();
  }
}
window.ExtoreWebMCP?.configure({
  api,
  getContext: () => ({
    page:
      {
        "/": "home",
        "/receipt": "receipt",
        "/admin": "admin",
        "/staff": "staff",
      }[location.pathname] || "home",
    role,
    product: currentProduct,
    currentToken,
    tab,
    cardProductId,
    queueProductId,
    queueProduct,
    permissions,
    productId: managedProductId,
    linkExpires: managementExpires,
  }),
  actions: {
    exchange: exchangeCode,
    redeem: submitRedemption,
    receipt: readReceipt,
    reveal: revealReceipt,
    destroy: destroyReceipt,
    navigate,
    selectQueue: async (productId, options = {}) => {
      const available = await api(
        "/manage/products",
        undefined,
        "GET",
        options,
      );
      if (!available.some((p) => p.id === productId))
        throw new Error("无权访问此商品队列");
      queueProductId = productId;
      await navigate(role === "staff" ? "/staff" : "/admin", "jobs");
      return { product_id: queueProductId };
    },
    refreshUI: async () => {
      if (location.pathname === "/admin") await admin();
      else if (location.pathname === "/staff") await staff();
      else if (location.pathname === "/receipt" && currentToken)
        await readReceipt();
      else await home();
    },
  },
});
start();
