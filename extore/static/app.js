"use strict";
const $ = (s) => document.querySelector(s),
  app = $("#app");
const preferences = window.ExtorePreferences;
let lang = preferences.resolved.language,
  currentToken = "",
  currentProduct = null,
  currentVariant = null,
  receiptViewKey = "",
  receiptMotion = null,
  waitingAnimationPaused = false,
  receiptGeneration = 0,
  timer = null,
  role = null,
  permissions = [],
  managedProductId = null,
  managementName = "",
  managementExpires = null,
  managementMaxUses = 1,
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
async function fileResponse(path, options = {}) {
  const response = await fetch("/api" + path, options);
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : tr("文件操作失败", "File operation failed"));
  }
  return response;
}
async function uploadMultipart(path, fields, file, options = {}) {
  if (!file || file.size > 20 * 1024 * 1024)
    throw new Error(tr("每个文件最多 20 MiB。", "Each file can be at most 20 MiB."));
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.append(key, value);
  body.append("file", file, file.name || "attachment");
  return (await fileResponse(path, { method: "POST", body, signal: options.signal })).json();
}
async function selectedJobForFile(definition, options = {}) {
  if (tab !== "jobs" || queueProductId !== definition.product_id || !permitted("queue.view"))
    throw new Error("请先打开对应的商品队列。");
  const query = new URLSearchParams({ product_id: definition.product_id, job_id: definition.job_id, limit: "1" });
  const jobs = await api("/manage/jobs?" + query, undefined, "GET", options);
  if (!jobs.some((job) => job.id === definition.job_id && job.product_id === definition.product_id))
    throw new Error("任务不属于当前商品队列。");
}
async function uploadFile(definition, options = {}) {
  const context = receiptRequestContext(options);
  const loadId = queueLoadId;
  const productId = queueProductId;
  const jobScope = definition.scope === "job";
  const active = () => !options.signal?.aborted && (jobScope ? loadId === queueLoadId && queueProductId === productId && tab === "jobs" : context.active());
  if (!active()) throw new Error("文件操作上下文已失效。");
  if (jobScope) {
    if (!permitted("queue.process")) throw new Error("无权上传交付文件。");
    await selectedJobForFile(definition, options);
  } else if (definition.scope !== "customer" || !currentToken || location.pathname !== "/receipt") {
    throw new Error("请先验证卡密。");
  }
  const encoded = definition.base64;
  if (typeof encoded !== "string" || encoded.length > Math.ceil(20 * 1024 * 1024 / 3) * 4 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))
    throw new Error("文件必须是有效 Base64，且不超过 20 MiB。");
  let raw;
  try { raw = atob(encoded); } catch { throw new Error("文件 Base64 不正确。"); }
  const bytes = Uint8Array.from(raw, (character) => character.charCodeAt(0));
  const file = new File([bytes], definition.filename || "attachment", { type: definition.content_type || "application/octet-stream" });
  if (!active()) throw new Error("文件操作上下文已失效。");
  const result = await uploadMultipart(jobScope ? "/manage/files/upload" : "/files/upload", jobScope ? { job_id: definition.job_id, field_key: definition.field_key } : { token: context.token, field_key: definition.field_key }, file, options);
  if (!active()) throw new Error("页面已切换，文件已上传但尚未提交任务。");
  return result;
}
async function readFile(definition, options = {}) {
  const loadId = queueLoadId;
  await selectedJobForFile(definition, options);
  const metadata = await api("/manage/files?" + new URLSearchParams({ job_id: definition.job_id }), undefined, "GET", options);
  const file = metadata.find((item) => item.id === definition.file_id && item.job_id === definition.job_id);
  if (!file) throw new Error("文件不属于此任务。");
  const limit = Math.min(1048576, definition.max_bytes || 1048576);
  if (file.size > limit) throw new Error("文件超过 AI 读取上限，请使用下载链接。");
  const response = await fileResponse("/manage/files/" + encodeURIComponent(file.id) + "/download", { signal: options.signal });
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length > limit) throw new Error("文件超过 AI 读取上限，请使用下载链接。");
  if (options.signal?.aborted || loadId !== queueLoadId || queueProductId !== definition.product_id || tab !== "jobs") throw new Error("文件操作上下文已失效。");
  let raw = "";
  for (let offset = 0; offset < bytes.length; offset += 16384)
    raw += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
  return { file_id: file.id, filename: file.filename, content_type: file.content_type, size: bytes.length, base64: btoa(raw) };
}
async function downloadDeliveryFile(file) {
  const context = receiptRequestContext();
  const response = await fileResponse("/files/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: context.token, file_id: file.id }) });
  const blob = await response.blob();
  if (!context.active()) return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
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
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = "";
  currentToken = "";
  currentProduct = null;
  currentVariant = null;
  const list = await api("/products");
  app.innerHTML = `<section class="intro home-paper-stack"><section class="intro-copy public-card" data-home-paper="products" tabindex="0" aria-label="${tr("公开商品", "Public products")}"><h1>${list.length ? tr("公开商品", "Public products") : tr("暂无公开商品", "No public products")}</h1><div class="paper-divider" aria-hidden="true"></div>${list.length ? `<p class="caption">${tr("选择商品了解详情，兑换需有效卡密。", "Browse the details. A valid code is required to redeem.")}</p><div class="home-product-list" tabindex="0" role="region" aria-label="${tr("公开商品列表，可上下滚动", "Public product list, scroll vertically")}">${list.map((p, i) => `<article class="product-row">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${esc(p.name)}</h3><p>${tr(p.delivery === "service" ? "服务兑换" : "商品领取", p.delivery === "service" ? "Service" : "Digital delivery")} · ${tr(p.mode === "manual" ? "队列处理" : "自动处理", p.mode === "manual" ? "Queue processing" : "Automatic")}</p></div><button class="secondary" data-product="${i}">${tr("查看详情", "Details")}</button></article>`).join("")}</div>` : ""}</section><section class="exchange" data-home-paper="redeem" tabindex="0" aria-label="${tr("卡密兑换", "Redeem a code")}"><h2>${tr("开始兑换", "Redeem your code")}</h2><p>${tr("请使用购买商品时获得的卡密。", "Use the code you received with your purchase.")}</p><form id="form"><div class="field"><label for="code">${tr("兑换卡密", "Redemption code")}</label><input class="code" id="code" placeholder="XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX" required autocomplete="off" spellcheck="false" maxlength="128"></div><button type="submit" class="full">${tr("验证并继续", "Verify and continue")} →</button><div id="error" class="error" role="alert"></div></form><div class="footnote">${tr("已有领取链接？直接打开即可查看处理进度。", "Already have a receipt link? Open it to check progress.")}</div></section></section>`;
  form(() => exchangeCode($("#code").value));
  window.ExtoreMotion?.mountHome(app);
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
  const generation = receiptGeneration;
  return {
    token,
    active: () =>
      receiptGeneration === generation &&
      currentToken === token &&
      location.pathname === pathname &&
      location.hash === hash &&
      !options.signal?.aborted,
  };
}
async function exchangeCode(code, options = {}) {
  const context = receiptRequestContext(options);
  const generation = receiptGeneration;
  const data = await api("/exchange", { code }, "POST", options);
  if (!context.active()) return data;
  const render = () => {
    if (!context.active()) return;
    currentToken = data.token;
    currentProduct = data.product;
    currentVariant = data.variant || data.job?.variant || null;
    history.pushState({}, "", "/receipt#" + currentToken);
    data.job ? renderReceipt(data.job) : redemptionForm();
  };
  if (window.ExtoreMotion && app.querySelector?.(".exchange"))
    await window.ExtoreMotion.transition(app, render, () => context.active() || (!options.signal?.aborted && receiptGeneration === generation && currentToken === data.token && location.pathname === "/receipt" && location.hash === "#" + data.token));
  else render();
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
    currentVariant = result.variant || result.job?.variant || null;
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
          const file = (result.files || []).find((item) => item.id === value);
          return `<div class="delivery-field"><h3>${esc(localized(definition?.label) || key)}</h3>${file ? `<button class="secondary" data-delivery-file="${esc(file.id)}">${tr("下载文件", "Download")} · ${esc(file.filename)}</button><p class="caption">${Math.ceil(file.size / 1024)} KiB</p>` : `<pre class="result">${esc(value)}</pre>`}</div>`;
        })
        .join("");
    }
    for (const file of result.files || [])
      on('[data-delivery-file="' + file.id + '"]', () => downloadDeliveryFile(file));
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
    // Invalidate responses issued before deletion, even on this same receipt.
    receiptGeneration++;
    const destroyedContext = receiptRequestContext(options);
    const content = $("#content");
    if (content) content.innerHTML = "";
    $("#reveal")?.remove();
    $("#destroy")?.remove();
    try {
      await readReceipt(options);
    } catch {
      if (destroyedContext.active())
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
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = "";
  window.ExtoreWebMCP?.refresh();
  const p = currentProduct;
  app.innerHTML = `<div class="narrow"><h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("填写信息", "Your details")}</div><div class="step">${tr("领取商品", "Collect")}</div></div>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="">` : ""}<div class="markdown">${md(p.description)}</div><div class="panel"><h2>${tr("确认兑换信息", "Confirm your details")}</h2><form id="form">${p.parameters.map((f) => `<div class="field"><label for="param-${f.key}">${esc(localized(f.label))}${f.required ? " *" : ""}</label>${f.type === "file" ? `<input id="param-${f.key}" type="file" ${f.required ? "required" : ""}><p class="caption">${tr("每个文件最多 20 MiB；提交后处理者可以下载。", "Up to 20 MiB per file. The operator can download it after submission.")}</p>` : f.type === "textarea" ? `<textarea id="param-${f.key}" ${f.required ? "required" : ""} maxlength="10000"></textarea>` : `<input id="param-${f.key}" type="${f.type}" ${f.type === "number" ? 'step="any"' : ""} ${f.required ? "required" : ""} maxlength="10000">`}</div>${localized(f.description) ? `<details ${f.collapsed ? "" : "open"}><summary>${tr("填写说明", "Instructions")}</summary><div class="markdown">${md(localized(f.description))}</div></details>` : ""}`).join("")}<button type="submit" class="full">${tr("确认兑换", "Confirm redemption")}</button><p class="caption">${tr("提交后会创建兑换任务，请保存领取链接。", "Save your receipt link after submitting.")}</p><div id="error" class="error" role="alert"></div></form></div></div>`;
  form(async () => {
    const context = receiptRequestContext();
    const params = {};
    for (const field of p.parameters) {
      const input = $("#param-" + field.key);
      if (field.type === "file") {
        const file = input.files[0];
        params[field.key] = file ? (await uploadMultipart("/files/upload", { token: context.token, field_key: field.key }, file)).id : "";
      } else params[field.key] = input.value;
      if (!context.active()) return;
    }
    await submitRedemption(params);
  });
}
function fulfillmentStepsMarkup(j) {
  const steps = j.steps || [];
  const firstPending = steps.findIndex((step) => !step.done);
  return steps.length ? `<ol class="fulfillment-steps">${steps.map((step, i) => `<li class="${step.done ? "done" : i === firstPending && j.state === "processing" ? "current" : "pending"}"><span class="step-mark" aria-hidden="true">${step.done ? "✓" : i + 1}</span><span>${esc(localized(step.label))}<small>${step.done ? tr("已完成", "Done") : i === firstPending && j.state === "processing" ? tr("进行中", "In progress") : tr("等待处理", "Pending")}</small></span></li>`).join("")}</ol>` : "";
}
function reminderMarkup(j) {
  const email = j.support_email || currentProduct?.support_email || "";
  if (!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email)) return "";
  const subject = tr("兑换进度询问 · ", "Redemption status · ") + j.id;
  const body = tr("你好，请帮我查看这项任务的进度。", "Hello, could you check the progress of this task?") + "\n\n" + currentProduct.name + "\n" + tr("任务号：", "Task: ") + j.id;
  return `<div class="support-contact"><a class="remind-link" href="mailto:${encodeURIComponent(email)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}">${tr("催一下", "Ask for an update")} ↗</a><span class="caption">${esc(email)}</span></div>`;
}
function updateReceiptView(j) {
  const waiting = ["queued", "processing"].includes(j.state);
  const percent = Math.max(0, Math.min(100, Number(j.progress) || 0));
  const message = $("#customer-message");
  if (message) message.textContent = j.message || tr(j.state === "queued" ? "已进入处理队列。" : "我们会在这里更新处理结果。", j.state === "queued" ? "Your task is in the queue." : "Updates will appear here.");
  const position = $("#queue-position");
  if (position) {
    position.hidden = !waiting;
    const place = j.queue_position || (Number(j.queue_ahead) || 0) + 1;
    position.textContent = tr("当前队列第 " + place + " 位 · 前面还有 " + (j.queue_ahead || 0) + " 个任务", "Position " + place + " · " + (j.queue_ahead || 0) + " tasks ahead");
  }
  const steps = $("#fulfillment-steps");
  if (steps) steps.innerHTML = fulfillmentStepsMarkup(j);
  const bar = $(".progress span");
  if (bar) bar.style.transform = "scaleX(" + percent / 100 + ")";
  $(".progress")?.setAttribute("aria-valuenow", String(percent));
  if ($("#progress-label")) $("#progress-label").textContent = percent + "%";
}
function pollReceipt(j) {
  if (!["queued", "processing"].includes(j.state)) return;
  const context = receiptRequestContext();
  timer = setTimeout(async () => {
    if (!context.active()) return;
    try { await readReceipt(); }
    catch (e) {
      if (context.active()) {
        toast(e.message);
        pollReceipt(j);
      }
    }
  }, document.hidden ? 15000 : 2500);
}
function renderReceipt(j) {
  stopPoll();
  window.ExtoreWebMCP?.refresh();
  const p = currentProduct;
  const waiting = ["processing", "queued"].includes(j.state);
  const viewKey = JSON.stringify([currentToken, j.id, j.state, j.can_retry, j.delivery, j.view_policy, p.name, j.support_email || p.support_email, lang, j.variant || currentVariant]);
  if (receiptViewKey === viewKey) {
    updateReceiptView(j);
    pollReceipt(j);
    return;
  }
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = viewKey;
  const variant = j.variant || currentVariant;
  app.innerHTML = `<div class="narrow ${waiting ? "receipt-waiting" : ""}"><h1>${esc(p.name)}</h1>${variant?.name ? `<p class="caption">${tr("规格：", "Variant: ")}${esc(variant.name)}</p>` : ""}<div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("信息已提交", "Details submitted")}</div><div class="step ${j.state === "succeeded" ? "active" : ""}">${tr("领取商品", "Collect")}</div></div><section class="panel receipt-panel">${waiting ? '<div class="paper-divider waiting-top" aria-hidden="true"></div>' : ""}<div class="section-head"><h2>${tr("兑换进度", "Redemption progress")}</h2>${status(j)}</div><p id="customer-message"></p><p id="queue-position" class="caption"></p><div id="fulfillment-steps"></div>${waiting ? `<div class="progress" role="progressbar" aria-label="${tr("处理进度", "Processing progress")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${j.progress}"><span></span></div><p id="progress-label" class="caption"></p>${window.ExtoreMotion?.waitingMarkup(lang) || ""}<button id="motion-toggle" class="secondary motion-toggle" aria-pressed="${waitingAnimationPaused}">${waitingAnimationPaused ? tr("播放动画", "Play animation") : tr("暂停动画", "Pause animation")}</button>${reminderMarkup(j)}` : ""}<div id="content"></div><div class="actions">${j.state === "succeeded" && j.delivery === "content" ? `<button id="reveal">${tr(j.view_policy === "once" ? "领取内容（仅一次）" : "查看交付内容", j.view_policy === "once" ? "Reveal once" : "View your goods")}</button>` : ""}${j.can_retry ? `<button id="retry">${tr("重新填写并重试", "Update details and retry")}</button>` : ""}${j.state === "succeeded" ? `<button id="destroy" class="danger">${tr("立即销毁", "Destroy now")}</button>` : ""}</div><div id="error" class="error" role="alert"></div>${j.state === "destroyed" ? `<p class="caption">${tr("内容已永久删除，此链接无法再领取。", "The content has been deleted. This link can no longer reveal it.")}</p>` : ""}${waiting ? '<div class="paper-divider waiting-bottom" aria-hidden="true"></div>' : ""}</section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(location.origin + "/receipt#" + currentToken)}</p><button id="copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("有效期 30 天。链接是领取凭证，请勿转发给他人。", "Valid for 30 days. Anyone with this link can access the receipt.")}</p></div></div>`;
  updateReceiptView(j);
  if (waiting && window.ExtoreMotion) {
    receiptMotion = window.ExtoreMotion.mount(app);
    receiptMotion.setPaused(waitingAnimationPaused);
  }
  on("#motion-toggle", () => {
    waitingAnimationPaused = !waitingAnimationPaused;
    receiptMotion?.setPaused(waitingAnimationPaused);
    $("#motion-toggle").setAttribute("aria-pressed", String(waitingAnimationPaused));
    $("#motion-toggle").textContent = waitingAnimationPaused ? tr("播放动画", "Play animation") : tr("暂停动画", "Pause animation");
  });
  on("#reveal", async () => {
    if (j.view_policy === "once" && !confirm(tr("内容只能打开一次，打开后请自行保存。继续领取？", "This content can only be revealed once. Save it after opening. Continue?"))) return;
    await revealReceipt();
  });
  on("#destroy", async () => {
    if (!confirm(tr("永久删除交付内容并关闭领取。继续销毁？", "Permanently delete the delivery and disable access?"))) return;
    await destroyReceipt();
  });
  on("#retry", redemptionForm);
  on("#copy", async () => {
    const copied = await writeClipboard(location.origin + "/receipt#" + currentToken);
    toast(copied ? tr("链接已复制", "Link copied") : tr("复制失败，请手动复制上方链接。", "Could not copy. Copy the link above manually."));
  });
  pollReceipt(j);
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
  managementMaxUses = auth.max_uses || 1;
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
  items.push(["sessions", "会话与审计"]);
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
  if (tab === "sessions") await renderSessions();
}
function productOptions() {
  return products
    .map((p) => `<option value="${p.id}">${esc(p.name)}</option>`)
    .join("");
}
async function writeClipboard(text) {
  if (globalThis.isSecureContext === false || !navigator.clipboard?.writeText)
    return false;
  try {
    await navigator.clipboard.writeText(String(text));
    return true;
  } catch {
    return false;
  }
}
window.ExtoreClipboard = Object.freeze({ writeText: writeClipboard });
async function copyManagementLink(url, input, isCurrent = () => true) {
  const copied = await writeClipboard(url);
  if (!isCurrent()) return copied;
  if (copied) {
    toast(tr("链接已复制", "Link copied"));
    return true;
  }
  if (input && input.isConnected !== false) {
    input.focus();
    input.select();
    input.setSelectionRange?.(0, input.value.length);
    toast(
      tr(
        "复制失败，已选中链接，请手动复制。",
        "Could not copy. The link is selected; copy it manually.",
      ),
    );
  } else {
    toast(tr("复制失败，请手动复制链接。", "Could not copy. Copy the link manually."));
  }
  return false;
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
    canManageCards: permitted("cards.manage"),
    lang,
    isCurrent: () =>
      loadId === queueLoadId &&
      location.pathname === pathname &&
      tab === "products",
    onSaved: (updated) => {
      products = updated;
    },
    notify: toast,
    copyManagementLink,
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
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th><input id="all" type="checkbox" aria-label="选择当前商品的全部任务"></th><th>任务</th><th>用户参数</th><th>状态 / 进度</th><th>尝试</th></tr></thead><tbody>${rows.map((j) => `<tr><td><input type="checkbox" name="job" value="${esc(j.id)}" aria-label="选择 ${esc(j.id)}"></td><td class="mono">${esc(j.id)}${j.variant?.name ? `<p class="caption">${esc(j.variant.name)}</p>` : ""}${j.queue_position ? `<p class="caption">队列第 ${j.queue_position} 位</p>` : ""}</td><td><pre>${esc(JSON.stringify(j.params, null, 2))}</pre>${(j.files || []).filter((file) => file.kind === "input").map((file) => `<p><a href="/api/manage/files/${encodeURIComponent(file.id)}/download">↓ ${esc(file.filename)}</a> <span class="caption">${Math.ceil(file.size / 1024)} KiB</span></p>`).join("")}</td><td>${status(j)}<p>${j.progress}% · ${esc(j.message)}</p>${j.steps?.length ? `<p class="caption">${j.steps.filter((step) => step.done).length} / ${j.steps.length} 步已完成</p>` : ""}</td><td>${j.attempt}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">这个商品暂无符合条件的任务。</div>'}
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
    const selectedRows = rows.filter((row) => selected.includes(row.id));
    const outputFields = selectedRows[0]?.outputs || selectedProduct.outputs || [];
    if (action === "succeed") {
      if (selectedRows.some((row) => JSON.stringify(row.outputs || selectedProduct.outputs || []) !== JSON.stringify(outputFields)))
        throw new Error("所选任务的交付定义不同，请分别交付。");
      if (selected.length > 1 && outputFields.some((field) => field.type === "file"))
        throw new Error("包含交付文件的任务请逐个完成，文件只属于自己的任务。");
    }
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>${action === "succeed" ? "批量完成" : "标记失败"} · ${esc(selectedProduct.name)} · ${selected.length} 个任务</h2>${textarea("batch-message", "处理说明")}${action === "succeed" ? outputFields.map((output, i) => `<div class="field"><label for="batch-output-${i}">${esc(localized(output.label))}${output.required ? " *" : ""}</label>${output.type === "file" ? `<input id="batch-output-${i}" type="file" ${output.required ? "required" : ""}><p class="caption">文件最多 20 MiB，只交付给此任务。</p>` : output.type === "textarea" ? `<textarea id="batch-output-${i}" maxlength="100000" ${output.required ? "required" : ""}></textarea>` : `<input id="batch-output-${i}" type="${output.type}" ${output.type === "number" ? 'step="any"' : ""} maxlength="100000" ${output.required ? "required" : ""}>`}</div>${localized(output.description) ? `<details ${output.collapsed ? "" : "open"}><summary>交付说明</summary><div class="markdown">${md(localized(output.description))}</div></details>` : ""}`).join("") || '<p class="caption">此商品只交付服务状态，无需填写内容。</p>' : '<div class="checks"><label><input id="batch-retry" type="checkbox">已确认未交付，允许顾客重试</label></div>'}<div class="actions"><button id="batch-submit">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", () => ($("#batch-form").innerHTML = ""));
    on("#batch-submit", async () => {
      const output = {};
      if (action === "succeed") {
        for (const [i, definition] of outputFields.entries()) {
          const input = $("#batch-output-" + i);
          if (!input.reportValidity()) return;
          if (definition.type === "file") {
            const file = input.files[0];
            output[definition.key] = file ? (await uploadMultipart("/manage/files/upload", { job_id: selected[0], field_key: definition.key }, file)).id : "";
          } else output[definition.key] = input.value;
          if (!active()) return;
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
    const selectedRows = rows.filter((row) => selected.includes(row.id));
    const plans = selectedRows.map((row) => row.steps || []);
    const plan = plans[0] || [];
    const planKey = (steps) => JSON.stringify(steps.map(({ id, label }) => ({ id, label })));
    if (plans.some((steps) => planKey(steps) !== planKey(plan)))
      throw new Error("所选任务的步骤不同，请分别更新进度。");
    const done = new Set(plans.flatMap((steps) => steps.filter((step) => step.done).map((step) => step.id)));
    const defaultSteps = selectedProduct.progress_steps || [];
    $("#batch-form").innerHTML = `<div class="panel"><h2>更新进度 · ${esc(selectedProduct.name)} · ${selected.length} 个任务</h2>${plan.length ? `<fieldset class="progress-checks"><legend>已完成的步骤</legend>${plan.map((step, i) => `<label><input type="checkbox" name="completed-step" value="${esc(step.id)}" ${done.has(step.id) ? "checked disabled" : ""}>${i + 1}. ${esc(localized(step.label))}</label>`).join("")}</fieldset><p class="caption">已完成的步骤不能取消。进度按完成步骤数自动计算。</p>` : `${textarea("batch-step-names", "定义步骤（每行一个，可留空）", defaultSteps.map((step) => localized(step.label)).join("\n"))}${field("batch-completed-count", "已完成前几步", 0, "number")}<p class="caption">设置后绑定到这些任务，后续只需勾选完成的步骤。</p>${field("batch-progress", "未定义步骤时的进度（0–99）", selectedRows[0]?.progress || 0, "number")}`}${textarea("batch-message", "给顾客的一句话", selectedRows.length === 1 ? selectedRows[0].message : "")}<div class="actions"><button id="batch-submit">更新进度</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", () => ($("#batch-form").innerHTML = ""));
    on("#batch-submit", async () => {
      if (!active()) return;
      const body = { ids: selected, action: "progress", message: $("#batch-message").value };
      if (plan.length) {
        body.completed_steps = [...document.querySelectorAll('[name="completed-step"]')].filter((input) => input.checked).map((input) => input.value);
      } else {
        const names = $("#batch-step-names").value.split("\n").map((name) => name.trim()).filter(Boolean);
        if (names.length > 30) throw new Error("最多定义 30 个步骤。");
        if (names.some((name) => name.length > 200)) throw new Error("步骤名称最多 200 字。");
        if (names.length) {
          const count = Number($("#batch-completed-count").value);
          if (!Number.isInteger(count) || count < 0 || count > names.length) throw new Error("已完成步骤数超出范围。");
          body.progress_steps = names.map((name, i) => {
            const original = defaultSteps[i];
            return original && localized(original.label) === name ? { id: original.id, label: original.label } : { id: "step_" + (i + 1), label: { [lang]: name } };
          });
          if (new Set(body.progress_steps.map((step) => step.id)).size !== names.length) throw new Error("步骤代码重复，请分别调整商品步骤。");
          body.completed_steps = body.progress_steps.slice(0, count).map((step) => step.id);
        } else {
          const percent = Number($("#batch-progress").value);
          if (!Number.isInteger(percent) || percent < 0 || percent > 99) throw new Error("进度须为 0–99 的整数。");
          body.progress = percent;
        }
      }
      await executeBatch(body);
      if (active()) await renderJobs(filter, productId);
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
  const generation = queueLoadId;
  const pathname = location.pathname;
  const renderRole = role;
  const active = () =>
    queueLoadId === generation &&
    location.pathname === pathname &&
    role === renderRole &&
    tab === "staff";
  const endpoint = renderRole === "staff" ? "/manage/links" : "/admin/staff";
  const availableProducts =
    renderRole === "staff" ? await api("/manage/products") : products;
  if (!active()) return;
  const links = await api(endpoint);
  if (!active()) return;
  products = availableProducts;
  const availablePermissions = Object.keys(permissionLabels).filter(permitted);
  const remainingDays = managementExpires
    ? Math.max(0, (managementExpires * 1000 - Date.now()) / 86400000)
    : 90;
  const defaultDays =
    role === "staff" ? Math.min(7, Math.floor(remainingDays * 100) / 100) : 7;
  $("#workspace").innerHTML =
    `<h2>商品管理链接</h2><p class="caption">一条链接只授权一个商品。可以分别授予队列、商品配置、卡密等权限；店长可获得该商品的完整管理权限。默认只能登录一次，已登录的会话可继续使用。链接是登录凭证，请私下交给管理者。</p>${role === "staff" ? '<p class="caption">你只能创建权限比自己更小的下级链接，有效期也不能超过自己的链接。</p>' : ""}<form id="form"><div class="grid">${field("staff-name", "管理者或链接名称")}<div class="field"><label for="staff-product">授权商品</label><select id="staff-product" ${role === "staff" ? "disabled" : ""}>${productOptions()}</select></div>${field("staff-days", "有效天数", defaultDays, "number")}${field("staff-max-uses", "可登录次数", 1, "number")}</div><fieldset class="permission-fields"><legend>权限范围</legend><div class="permission-presets"><button type="button" id="preset-view" class="secondary">只看队列</button><button type="button" id="preset-process" class="secondary">处理任务</button>${role === "admin" ? '<button type="button" id="preset-manager" class="secondary">店长：完全管理商品</button>' : ""}</div><div class="permission-grid">${availablePermissions.map((key) => `<label><input type="checkbox" name="link-permission" value="${key}" ${["queue.view", "queue.process"].includes(key) ? "checked" : ""}>${permissionLabels[key]}</label>`).join("")}</div><p class="caption">“创建下级管理链接”允许继续委派。下级必须少至少一项权限，不能扩大商品范围或有效期。撤销上级链接会同时撤销全部下级。</p></fieldset><button type="submit" class="full" ${products.length ? "" : "disabled"}>创建管理链接</button><div id="error" class="error" role="alert"></div></form><div id="staff-link"></div><div class="form-divider table-wrap"><table><thead><tr><th>管理链接</th><th>商品</th><th>权限</th><th>有效期</th><th>登录次数</th><th></th></tr></thead><tbody>${links.map((link) => `<tr><td>${esc(link.name)}${link.parent_id ? '<div class="caption">下级链接</div>' : ""}</td><td>${esc(products.find((p) => p.id === link.product_id)?.name || link.product_id)}</td><td>${(link.permissions || []).map((key) => esc(permissionLabels[key] || key)).join("<br>")}</td><td>${link.revoked ? "已撤销" : new Date(link.expires * 1000).toLocaleString()}</td><td>${link.uses || 0} / ${link.max_uses || 1}<p class="caption">剩余 ${link.remaining_uses ?? Math.max(0, (link.max_uses || 1) - (link.uses || 0))} 次</p></td><td>${!link.revoked ? `<button data-revoke="${esc(link.id)}" class="danger">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  $("#staff-max-uses").required = true;
  $("#staff-max-uses").min = "1";
  $("#staff-max-uses").max = String(role === "staff" ? managementMaxUses : 1000);
  $("#staff-max-uses").step = "1";
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
    const creationGeneration = queueLoadId;
    const creationPath = location.pathname;
    const creationRole = role;
    const active = () =>
      queueLoadId === creationGeneration &&
      location.pathname === creationPath &&
      role === creationRole &&
      tab === "staff";
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
      max_uses: Number($("#staff-max-uses").value),
      permissions: selected,
    });
    if (!active()) return;
    await renderStaff();
    if (!active()) return;
    $("#staff-link").innerHTML =
      `<div class="field"><label for="created-management-link">${tr("商品管理链接", "Product management link")}</label><input id="created-management-link" type="text" value="${esc(result.url)}" readonly autocomplete="off" spellcheck="false"></div><button id="copy-management-link" type="button" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("链接只显示一次。请现在保存并私下交付。", "This link is shown once. Save it now and share it privately.")}</p>`;
    const input = $("#created-management-link");
    on("#copy-management-link", () =>
      copyManagementLink(result.url, input, () => active() && input.isConnected),
    );
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
async function renderSessions() {
  const generation = queueLoadId;
  const viewRole = role;
  const active = () => generation === queueLoadId && role === viewRole && tab === "sessions";
  const prefix = role === "admin" ? "/admin" : "/manage";
  const [sessions, entries] = await Promise.all([api(prefix + "/sessions"), api(prefix + "/audit?limit=200")]);
  if (!active()) return;
  const date = (value) => value ? new Date(value * 1000).toLocaleString() : "—";
  $("#workspace").innerHTML = `<div class="section-head"><div><h2>会话管理</h2><p class="caption">${role === "admin" ? "查看全店会话，按需注销单个设备。" : permitted("links.delegate") ? "查看自己的会话和下级链接的会话，只限授权商品。" : "查看与注销自己的会话。"} 链接次数耗尽不会结束已有会话。</p></div><button id="sessions-refresh" class="secondary">刷新</button></div><div class="table-wrap"><table><thead><tr><th>登录来源</th><th>设备 / 地址</th><th>登录 / 最近活动</th><th>到期</th><th>状态</th><th></th></tr></thead><tbody>${sessions.map((session) => `<tr><td>${esc(session.link_name || (session.role === "admin" ? "商家 Passkey" : session.role === "bootstrap" ? "首次登录" : "商品管理链接"))}${session.product_name ? `<p class="caption">${esc(session.product_name)}</p>` : ""}${session.current ? '<p class="caption">当前会话</p>' : ""}</td><td class="session-client">${esc(session.ua || "未记录设备")}<p class="caption mono">${esc(session.ip || "—")}</p></td><td>${date(session.created)}<p class="caption">${date(session.last_seen)}</p></td><td>${date(session.expires)}</td><td>${session.revoked ? "已注销" : session.active ? "有效" : "已到期"}</td><td>${session.active ? `<button class="danger" data-end-session="${esc(session.id)}">${session.current ? "退出此会话" : "注销"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div><h2 class="form-divider">访问审计</h2><p class="caption">最近 200 条登录、链接使用、委派和注销记录。记录中不包含卡密或授权凭证。</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>目标</th></tr></thead><tbody>${entries.map((entry) => `<tr><td>${date(entry.created)}</td><td>${esc(entry.action)}</td><td class="mono">${esc(entry.actor)}</td><td class="mono">${esc(entry.target)}</td></tr>`).join("")}</tbody></table></div><div id="error" class="error" role="alert"></div>`;
  on("#sessions-refresh", renderSessions);
  document.querySelectorAll("[data-end-session]").forEach((button) => button.addEventListener("click", () => perform(async () => {
    if (!active()) return;
    const session = sessions.find((item) => item.id === button.dataset.endSession);
    if (!confirm(session?.current ? "退出当前会话？之后需要重新登录或取得新的授权链接。" : "注销此设备的会话？已完成的任务不受影响。")) return;
    const result = await api(prefix + "/sessions/" + encodeURIComponent(session.id), undefined, "DELETE");
    if (!active()) return;
    if (result.current) {
      acceptAuth({ role: null });
      toast("当前会话已注销。");
      await navigate(viewRole === "admin" ? "/admin" : "/staff");
    } else await renderSessions();
  }, button)));
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
  receiptMotion?.dispose();
  window.ExtoreMotion?.disposeHome?.(app);
  receiptMotion = null;
  receiptViewKey = "";
  queueLoadId++;
  window.ExtoreMotion?.cancel(app);
  window.ExtoreMotion?.mountHome?.(app)?.dispose();
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
    uploadFile,
    readFile,
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
