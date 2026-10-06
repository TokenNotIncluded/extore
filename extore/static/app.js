"use strict";
const $ = (s) => document.querySelector(s),
  app = $("#app");
let lang = localStorage.getItem("extore_language") || "zh-CN",
  currentToken = "",
  currentProduct = null,
  timer = null,
  role = null,
  tab = "products",
  products = [];
$("#language").value = lang;
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
async function api(path, body, method) {
  const response = await fetch("/api" + path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data;
  try {
    data = await response.json();
  } catch {
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
function navigate(path) {
  stopPoll();
  history.pushState({}, "", path);
  start();
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
$("#language").addEventListener("change", () => {
  lang = $("#language").value;
  localStorage.setItem("extore_language", lang);
  start();
});
window.addEventListener("popstate", start);

async function home() {
  const list = await api("/products");
  app.innerHTML = `<section class="intro"><div class="intro-copy"><h1>${tr("你的商品，<br>从这里领取。", "Your goods.<br>Ready to collect.")}</h1><p>${tr("输入卡密，填写必要的信息。<br>接下来的事，交给我们。", "Enter your redemption code and the required details.<br>We’ll take care of the rest.")}</p><div class="path"><span>${tr("验证卡密", "Verify code")}</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor"><path d="M3 10h13m-5-5 5 5-5 5"/></svg><span>${tr("确认信息", "Add details")}</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor"><path d="M3 10h13m-5-5 5 5-5 5"/></svg><span>${tr("领取商品", "Collect")}</span></div></div><section class="exchange"><h2>${tr("开始兑换", "Redeem your code")}</h2><p>${tr("请使用购买商品时获得的卡密。", "Use the code you received with your purchase.")}</p><form id="form"><div class="field"><label for="code">${tr("兑换卡密", "Redemption code")}</label><input class="code" id="code" placeholder="XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX" required autocomplete="off" spellcheck="false" maxlength="128"></div><button type="submit" class="full">${tr("验证并继续", "Verify and continue")} →</button><div id="error" class="error" role="alert"></div></form><div class="footnote">${tr("已有领取链接？直接打开即可查看处理进度。", "Already have a receipt link? Open it to check progress.")}</div></section></section>${list.length ? `<section class="catalog"><div class="section-head"><div><h2>${tr("可兑换商品", "Available products")}</h2><p>${tr("选择商品了解详情，兑换仍需有效卡密。", "Browse the details. A valid code is required to redeem.")}</p></div></div><div class="product-list">${list.map((p, i) => `<article class="product-row">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${esc(p.name)}</h3><p>${tr(p.delivery === "service" ? "服务兑换" : "商品领取", p.delivery === "service" ? "Service" : "Digital delivery")} · ${tr(p.mode === "manual" ? "人工处理" : "自动处理", p.mode === "manual" ? "Manual processing" : "Automatic")}</p></div><button class="secondary" data-product="${i}">${tr("查看详情", "Details")}</button></article>`).join("")}</div></section>` : ""}`;
  form(async () => {
    const data = await api("/exchange", { code: $("#code").value });
    currentToken = data.token;
    currentProduct = data.product;
    history.pushState({}, "", "/receipt#" + currentToken);
    data.job ? renderReceipt(data.job) : redemptionForm();
  });
  document
    .querySelectorAll("[data-product]")
    .forEach((b) =>
      b.addEventListener("click", () => publicDetail(list[+b.dataset.product])),
    );
}
function publicDetail(p) {
  app.innerHTML = `<div class="narrow"><button class="secondary" id="back">← ${tr("返回", "Back")}</button><div class="panel"><h1>${esc(p.name)}</h1>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="${esc(p.name)}">` : ""}<div class="markdown">${md(p.description)}</div><button id="return" class="full">${tr("输入卡密兑换", "Enter a code to redeem")}</button></div></div>`;
  on("#back", home);
  on("#return", home);
}
function redemptionForm() {
  stopPoll();
  const p = currentProduct;
  app.innerHTML = `<div class="narrow"><h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("填写信息", "Your details")}</div><div class="step">${tr("领取商品", "Collect")}</div></div>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="">` : ""}<div class="markdown">${md(p.description)}</div><div class="panel"><h2>${tr("确认兑换信息", "Confirm your details")}</h2><form id="form">${p.parameters.map((f) => `<div class="field"><label for="param-${f.key}">${esc(localized(f.label))}${f.required ? " *" : ""}</label>${f.type === "textarea" ? `<textarea id="param-${f.key}" ${f.required ? "required" : ""} maxlength="10000"></textarea>` : `<input id="param-${f.key}" type="${f.type}" ${f.type === "number" ? 'step="any"' : ""} ${f.required ? "required" : ""} maxlength="10000">`}</div>${localized(f.description) ? `<details ${f.collapsed ? "" : "open"}><summary>${tr("填写说明", "Instructions")}</summary><div class="markdown">${md(localized(f.description))}</div></details>` : ""}`).join("")}<button type="submit" class="full">${tr("确认兑换", "Confirm redemption")}</button><p class="caption">${tr("提交后会创建兑换任务，请保存领取链接。", "Save your receipt link after submitting.")}</p><div id="error" class="error" role="alert"></div></form></div></div>`;
  form(async () => {
    const params = Object.fromEntries(
      p.parameters.map((f) => [f.key, $("#param-" + f.key).value]),
    );
    renderReceipt(await api("/redeem", { token: currentToken, params }));
  });
}
function renderReceipt(j) {
  stopPoll();
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
    const r = await api("/receipt/reveal", { token: currentToken });
    $("#content").innerHTML = `<pre class="result">${esc(r.content)}</pre>`;
    if (j.view_policy === "once") $("#reveal").remove();
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
    await api("/receipt/destroy", { token: currentToken });
    renderReceipt((await api("/receipt", { token: currentToken })).job);
  });
  on("#retry", redemptionForm);
  on("#copy", async () => {
    await navigator.clipboard.writeText(
      location.origin + "/receipt#" + currentToken,
    );
    toast(tr("链接已复制", "Link copied"));
  });
  if (["queued", "processing"].includes(j.state))
    timer = setTimeout(async () => {
      try {
        const data = await api("/receipt", { token: currentToken });
        if (location.pathname === "/receipt") renderReceipt(data.job);
      } catch (e) {
        toast(e.message);
      }
    }, 2500);
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
  role = auth.role;
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
      admin();
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
function shell() {
  app.innerHTML = `<div class="admin-top"><div><h1>${role === "staff" ? "商品处理台" : "商家后台"}</h1><p class="muted">${role === "staff" ? "处理你被授权的商品任务。" : "管理商品、兑换任务与交付。"}</p></div><button id="logout" class="secondary">退出登录</button></div>${
    role === "admin"
      ? `<nav class="tabs" aria-label="后台导航">${[
          ["products", "商品"],
          ["jobs", "处理队列"],
          ["cards", "卡密"],
          ["staff", "员工授权"],
          ["events", "事件记录"],
          ["security", "Passkey"],
        ]
          .map(
            ([v, label]) =>
              `<button data-tab="${v}" class="${tab === v ? "active" : ""}">${label}</button>`,
          )
          .join("")}</nav>`
      : ""
  }<div id="workspace"></div>`;
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
  if (tab === "products") renderProducts();
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
function renderProducts() {
  $("#workspace").innerHTML =
    `<div class="section-head"><h2>商品</h2><button id="new-product">新建商品</button></div>${products.length ? `<div class="product-list">${products.map((p) => `<article class="product-row"><div class="product-icon">${icon}</div><div class="product-info"><h3>${esc(p.name)}</h3><p>${p.public ? "公开展示" : "仅持卡可见"} · ${{ manual: "人工处理", webhook: "Webhook", script: "Python 脚本" }[p.mode]} · ${p.delivery === "content" ? "内容交付" : "服务状态"}</p><div class="mono muted">${p.id}</div></div><button class="secondary" data-edit="${p.id}">配置</button></article>`).join("")}</div>` : '<div class="empty">还没有商品。先创建商品，再生成卡密。</div>'}`;
  on("#new-product", () => editProduct());
  document
    .querySelectorAll("[data-edit]")
    .forEach((b) =>
      b.addEventListener("click", () =>
        editProduct(products.find((p) => p.id === b.dataset.edit)),
      ),
    );
}
function editProduct(
  p = {
    name: "",
    description: "",
    parameters: [],
    mode: "manual",
    delivery: "content",
    view_policy: "repeat",
    allow_retry: true,
    max_attempts: 3,
  },
) {
  let params = structuredClone(p.parameters);
  const w = $("#workspace");
  w.innerHTML = `<div class="section-head"><h2>${p.id ? "配置商品" : "新建商品"}</h2><button id="cancel" class="secondary">返回商品</button></div><form id="form"><div class="grid">${field("p-name", "商品名称", p.name)}${select(
    "p-mode",
    "处理方式",
    [
      ["manual", "人工队列"],
      ["webhook", "外部平台 Webhook"],
      ["script", "Python 脚本"],
    ],
    p.mode,
  )}${field("p-logo", "商品 Logo URL（HTTPS）", p.logo || "")}${field("p-image", "商品图片 URL（HTTPS）", p.image || "")}${select(
    "p-delivery",
    "交付类型",
    [
      ["content", "交付内容 / 链接"],
      ["service", "只返回服务状态"],
    ],
    p.delivery,
  )}${select(
    "p-view",
    "内容查看规则",
    [
      ["repeat", "允许重复查看"],
      ["once", "仅允许领取一次"],
    ],
    p.view_policy,
  )}</div>${textarea("p-description", "商品描述（Markdown）", p.description)}<div class="checks"><label><input id="p-public" type="checkbox" ${p.public ? "checked" : ""}>公开展示商品</label><label><input id="p-retry" type="checkbox" ${p.allow_retry ? "checked" : ""}>允许明确失败后重试</label></div>${field("p-attempts", "最多尝试次数", p.max_attempts, "number")}<div class="form-divider"><h3>发货对接</h3><p class="caption">人工商品无需填写。脚本须由商家通过服务器安装。Webhook 地址只允许 HTTPS 公网地址。</p><div class="grid">${field("p-url", "Webhook 接收地址", p.webhook_url || "")}${field("p-secret", "Webhook 签名密钥（至少 32 字符）", p.webhook_secret || "", "password")}${field("p-script", "Python 脚本名称（不含 .py）", p.script || "")}</div></div><div class="form-divider"><div class="section-head"><h3>顾客填写的参数</h3><button type="button" id="add-param" class="secondary">添加参数</button></div><div id="parameters"></div></div><button type="submit" class="full">保存商品</button><div id="error" class="error" role="alert"></div></form>`;
  const capture = () => {
    params = params.map((f, i) => ({
      ...f,
      key: $("#f-key-" + i).value,
      label: JSON.parse($("#f-label-" + i).value),
      description: JSON.parse($("#f-description-" + i).value),
      type: $("#f-type-" + i).value,
      required: $("#f-required-" + i).checked,
      collapsed: $("#f-collapsed-" + i).checked,
    }));
  };
  const draw = () => {
    $("#parameters").innerHTML = params
      .map(
        (f, i) =>
          `<div class="parameter"><h3>参数 ${i + 1}<button type="button" class="danger" data-remove="${i}">删除</button></h3><div class="grid">${field("f-key-" + i, "代码名（传给程序）", f.key)}${select(
            "f-type-" + i,
            "输入类型",
            [
              ["text", "文本"],
              ["email", "邮箱"],
              ["textarea", "多行文本"],
              ["number", "数字"],
            ],
            f.type,
          )}</div>${textarea("f-label-" + i, "显示名称（语言 → 文本 JSON）", JSON.stringify(f.label, null, 2))}${textarea("f-description-" + i, "Markdown 教程（语言 → 文本 JSON）", JSON.stringify(f.description || {}, null, 2))}<div class="checks"><label><input id="f-required-${i}" type="checkbox" ${f.required ? "checked" : ""}>必填</label><label><input id="f-collapsed-${i}" type="checkbox" ${f.collapsed ? "checked" : ""}>默认折叠教程</label></div></div>`,
      )
      .join("");
    document.querySelectorAll("[data-remove]").forEach((b) =>
      b.addEventListener("click", () =>
        perform(() => {
          capture();
          params.splice(+b.dataset.remove, 1);
          draw();
        }),
      ),
    );
  };
  draw();
  on("#cancel", renderProducts);
  on("#add-param", () => {
    capture();
    params.push({
      key: "field_" + (params.length + 1),
      label: { "zh-CN": "参数名称", en: "Parameter" },
      description: { "zh-CN": "" },
      type: "text",
      required: true,
      collapsed: true,
    });
    draw();
  });
  form(async () => {
    capture();
    const body = {
      name: $("#p-name").value,
      description: $("#p-description").value,
      logo: $("#p-logo").value,
      image: $("#p-image").value,
      public: $("#p-public").checked,
      mode: $("#p-mode").value,
      delivery: $("#p-delivery").value,
      view_policy: $("#p-view").value,
      allow_retry: $("#p-retry").checked,
      max_attempts: +$("#p-attempts").value,
      parameters: params,
      webhook_url: $("#p-url").value,
      webhook_secret: $("#p-secret").value,
      script: $("#p-script").value,
    };
    await api(
      "/admin/products" + (p.id ? "/" + p.id : ""),
      body,
      p.id ? "PUT" : "POST",
    );
    products = await api("/admin/products");
    renderProducts();
    toast("商品已保存");
  });
}
async function renderJobs(filter = "") {
  const rows = await api("/manage/jobs" + (filter ? "?state=" + filter : ""));
  $("#workspace").innerHTML =
    `<div class="toolbar"><select id="job-state" aria-label="任务状态"><option value="">全部状态</option>${Object.entries(
      states,
    )
      .map(
        ([k, v]) =>
          `<option value="${k}" ${filter === k ? "selected" : ""}>${v}</option>`,
      )
      .join(
        "",
      )}</select><button id="refresh" class="secondary">刷新</button><button id="claim">领取选中任务</button><button id="progress-update" class="secondary">更新进度</button><button id="complete" class="secondary">批量完成</button><button id="fail" class="secondary">标记失败</button>${role === "admin" ? '<button id="release" class="secondary">核实后允许重试</button>' : ""}</div><p class="caption">批量操作全部成功才提交。请先领取任务，再处理。批量完成会给所选任务相同的交付内容。</p>${rows.length ? `<div class="table-wrap"><table><thead><tr><th><input id="all" type="checkbox" aria-label="选择全部"></th><th>商品 / 任务</th><th>用户参数</th><th>状态 / 进度</th><th>尝试</th></tr></thead><tbody>${rows.map((j) => `<tr><td><input type="checkbox" name="job" value="${j.id}" aria-label="选择 ${j.id}"></td><td>${esc(j.product_name)}<div class="mono muted">${j.id}</div><div class="caption">${j.mode === "manual" ? "人工" : "自动处理"}</div></td><td><pre>${esc(JSON.stringify(j.params, null, 2))}</pre></td><td>${status(j)}<p>${j.progress}% · ${esc(j.message)}</p></td><td>${j.attempt}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">暂无符合条件的任务。</div>'}<div id="batch-form"></div><div id="error" class="error" role="alert"></div>`;
  $("#job-state").addEventListener("change", () =>
    perform(() => renderJobs($("#job-state").value)),
  );
  on("#refresh", () => renderJobs(filter));
  $("#all")?.addEventListener("change", () =>
    document
      .querySelectorAll("[name=job]")
      .forEach((c) => (c.checked = $("#all").checked)),
  );
  const ids = () => {
    const values = [...document.querySelectorAll("[name=job]:checked")].map(
      (c) => c.value,
    );
    if (!values.length) throw new Error("请先选择任务");
    return values;
  };
  on("#claim", async () => {
    await api("/manage/batch", { ids: ids(), action: "claim" });
    await renderJobs(filter);
  });
  on("#release", async () => {
    if (!confirm("确认已核实外部平台没有交付？允许重试可能再次调用发货程序。"))
      return;
    await api("/manage/batch", { ids: ids(), action: "retry" });
    await renderJobs(filter);
  });
  function finish(action) {
    const selected = ids();
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>${action === "succeed" ? "批量完成" : "标记失败"} · ${selected.length} 个任务</h2>${textarea("batch-message", "处理说明")}${action === "succeed" ? textarea("batch-content", "交付内容（内容型商品必填，服务型可留空）") : '<div class="checks"><label><input id="batch-retry" type="checkbox">已确认未交付，允许顾客重试</label></div>'}<div class="actions"><button id="batch-submit">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", () => ($("#batch-form").innerHTML = ""));
    on("#batch-submit", async () => {
      await api("/manage/batch", {
        ids: selected,
        action,
        message: $("#batch-message").value,
        content: $("#batch-content")?.value || null,
        retryable: $("#batch-retry")?.checked || false,
      });
      await renderJobs(filter);
    });
  }
  on("#progress-update", () => {
    const selected = ids();
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>更新进度</h2>${field("batch-progress", "进度（0–99）", 30, "number")}${field("batch-message", "处理说明")}<button id="batch-submit" class="full">提交进度</button></div>`;
    on("#batch-submit", async () => {
      await api("/manage/batch", {
        ids: selected,
        action: "progress",
        progress: +$("#batch-progress").value,
        message: $("#batch-message").value,
      });
      await renderJobs(filter);
    });
  });
  on("#complete", () => finish("succeed"));
  on("#fail", () => finish("fail"));
}
async function renderCards() {
  const cards = await api("/admin/cards");
  $("#workspace").innerHTML =
    `<h2>发行卡密</h2><p class="caption">卡密具有 160 位随机强度。原文只在生成时显示，请立即保存。</p><form id="form"><div class="grid"><div class="field"><label for="card-product">对应商品</label><select id="card-product" required>${productOptions()}</select></div>${field("card-count", "数量", 10, "number")}</div><button type="submit" class="full" ${products.length ? "" : "disabled"}>生成卡密</button><div id="error" class="error" role="alert"></div></form><div id="codes" class="secret-output"></div><div class="form-divider"><h3>最近发行</h3><div class="table-wrap"><table><thead><tr><th>卡密 ID</th><th>商品</th><th>状态</th><th></th></tr></thead><tbody>${cards.map((c) => `<tr><td class="mono">${c.id}</td><td>${esc(products.find((p) => p.id === c.product_id)?.name || c.product_id)}</td><td>${esc(c.state)}</td><td>${c.state === "ready" ? `<button class="danger" data-revoke-card="${c.id}">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>`;
  form(async () => {
    const r = await api("/admin/cards", {
      product_id: $("#card-product").value,
      count: +$("#card-count").value,
    });
    $("#codes").innerHTML =
      '<label for="generated">本次生成的卡密（离开页面后不能找回）</label><textarea id="generated" readonly></textarea><button id="download" class="secondary">下载文本</button>';
    $("#generated").value = r.codes.join("\n");
    on("#download", () => {
      const url = URL.createObjectURL(
        new Blob([r.codes.join("\n")], { type: "text/plain" }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = "extore-codes.txt";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  });
  document.querySelectorAll("[data-revoke-card]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        if (!confirm("撤销后无法兑换，继续？")) return;
        await api("/admin/cards/" + b.dataset.revokeCard + "/revoke", {});
        renderCards();
      }, b),
    ),
  );
}
async function renderStaff() {
  const staff = await api("/admin/staff");
  $("#workspace").innerHTML =
    `<h2>员工授权</h2><p class="caption">每条链接只允许处理一个人工商品。持有链接即可登录，请单独发给对应员工。</p><form id="form"><div class="grid">${field("staff-name", "员工或授权名称")}<div class="field"><label for="staff-product">授权商品</label><select id="staff-product">${products
      .filter((p) => p.mode === "manual")
      .map((p) => `<option value="${p.id}">${esc(p.name)}</option>`)
      .join(
        "",
      )}</select></div>${field("staff-days", "有效天数", 7, "number")}</div><button type="submit" class="full">创建授权链接</button><div id="error" class="error" role="alert"></div></form><div id="staff-link"></div><div class="form-divider"><table><thead><tr><th>员工</th><th>商品</th><th>有效期</th><th></th></tr></thead><tbody>${staff.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(products.find((p) => p.id === s.product_id)?.name || "")}</td><td>${s.revoked ? "已撤销" : new Date(s.expires * 1000).toLocaleString()}</td><td>${!s.revoked ? `<button data-revoke="${s.id}" class="danger">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  form(async () => {
    const r = await api("/admin/staff", {
      name: $("#staff-name").value,
      product_id: $("#staff-product").value,
      days: +$("#staff-days").value,
    });
    $("#staff-link").innerHTML =
      `<pre class="result">${esc(r.url)}</pre><p class="caption">链接只显示一次。撤销授权会立即退出该员工的全部会话。</p>`;
  });
  document.querySelectorAll("[data-revoke]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        if (!confirm("撤销授权并将员工未完成任务放回队列？")) return;
        await api("/admin/staff/" + b.dataset.revoke + "/revoke", {});
        await renderStaff();
      }),
    ),
  );
}
async function renderEvents() {
  const rows = await api("/admin/events");
  $("#workspace").innerHTML =
    `<div class="section-head"><h2>事件记录</h2><button id="refresh" class="secondary">刷新</button></div><p class="caption">投递失败最多自动重试 8 次。外部平台须按事件 ID 去重。</p><div class="table-wrap"><table><thead><tr><th>事件</th><th>时间</th><th>Webhook</th><th>尝试</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.type)}<div class="mono muted">${r.id}</div></td><td>${new Date(r.created * 1000).toLocaleString()}</td><td>${esc(r.webhook_state || "未配置")}<div class="caption">${esc(r.error)}</div></td><td>${r.attempts ?? "—"}</td><td>${r.webhook_state === "dead" ? `<button data-event="${r.id}" class="secondary">重新投递</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  on("#refresh", renderEvents);
  document.querySelectorAll("[data-event]").forEach((b) =>
    b.addEventListener("click", () =>
      perform(async () => {
        await api("/admin/events/" + b.dataset.event + "/retry", {});
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
      '<div class="narrow"><h1>员工授权已失效</h1><p>请通过商家发给你的授权链接进入。</p></div>';
    return;
  }
  role = "staff";
  tab = "jobs";
  shell();
  await renderJobs();
}
async function start() {
  stopPoll();
  app.innerHTML =
    '<div class="loading">' + tr("正在加载…", "Loading…") + "</div>";
  try {
    if (location.pathname === "/admin") await admin();
    else if (location.pathname === "/staff") await staff();
    else if (location.pathname === "/receipt") {
      currentToken = location.hash.slice(1);
      if (!currentToken)
        throw new Error(
          tr(
            "领取链接缺少凭证，请重新输入卡密。",
            "This receipt link is missing its token. Enter your code again.",
          ),
        );
      const data = await api("/receipt", { token: currentToken });
      currentProduct = data.product;
      data.job ? renderReceipt(data.job) : redemptionForm();
    } else await home();
  } catch (e) {
    app.innerHTML = `<div class="narrow"><h1>${tr("暂时无法打开", "Unable to open")}</h1><p>${esc(e.message)}</p><button id="back" class="full">${tr("返回兑换页", "Back to redemption")}</button></div>`;
    on("#back", () => navigate("/"));
  }
}
start();
