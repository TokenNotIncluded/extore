"use strict";
const $ = (s) => document.querySelector(s),
  app = $("#app");
const preferences = window.ExtorePreferences;
let lang = preferences.resolved.language,
  currentToken = "",
  currentBatch = null,
  batchSelection = "",
  batchRetryOnly = false,
  currentProduct = null,
  currentJob = null,
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
  managementMaxCLIUses = 1,
  managementRemainingCLIUses = 1,
  managementLinkId = null,
  tab = "products",
  products = [],
  cardProductId = "",
  queueProductId = "",
  queueView = "active",
  queueProduct = null,
  queueLoadId = 0;
let uploadLimitPromise = null;
let maxUploadFileBytes = 20 * 1024 * 1024;
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
  needs_input: "需补充信息",
  rejected: "已拒绝",
  destroyed: "已销毁",
};
const stateEn = {
  queued: "Queued",
  processing: "Processing",
  succeeded: "Completed",
  failed: "Failed",
  needs_input: "More details needed",
  rejected: "Rejected",
  destroyed: "Destroyed",
};
const status = (j) =>
  `<span class="status ${esc(j.state)}">${esc(lang === "en" ? stateEn[j.state] : states[j.state])}</span>`;
function splitCodes(value) {
  const seen = new Set();
  const codes = [];
  for (const part of String(value || "").trim().split(/[\s,，;；]+/)) {
    if (!part) continue;
    const key = part.toUpperCase().replace(/[-\s]/g, "");
    if (!key || seen.has(key)) continue;
    seen.add(key);
    codes.push(part);
  }
  return codes;
}
function receiptCardBody(context, extra = {}) {
  const body = { token: context.token, ...extra };
  if (context.cardId) body.card_id = context.cardId;
  return body;
}
function selectBatchCard(cardId = "", retryOnly = false) {
  if (batchSelection !== cardId || batchRetryOnly !== retryOnly) receiptGeneration++;
  batchSelection = cardId;
  batchRetryOnly = retryOnly;
}
function batchProduct(item) {
  return item?.product || currentBatch?.product || currentProduct;
}
function updateCodeCount() {
  const node = $("#code-count");
  if (!node) return;
  const count = splitCodes($("#code")?.value).length;
  node.textContent = count > 1
    ? tr(`已识别 ${count} 张卡密，将合并到同一个领取链接。`, `${count} codes recognized. They share one receipt link.`)
    : "";
}
async function pasteCodes() {
  let text = "";
  try {
    text = await navigator.clipboard.readText();
  } catch {
    throw new Error(tr("无法读取剪贴板，请在输入框里粘贴。", "Clipboard access was denied. Paste into the field instead."));
  }
  const incoming = String(text || "").trim();
  if (!incoming) throw new Error(tr("剪贴板是空的", "The clipboard is empty"));
  const field = $("#code");
  if (!field) return;
  const current = field.value.trim();
  field.value = current ? `${current}\n${incoming}` : incoming;
  updateCodeCount();
  field.focus();
}
function batchProgress(item) {
  if (!item?.job) return 0;
  if (["succeeded", "destroyed"].includes(item.job.state)) return 100;
  return Math.max(0, Math.min(100, Number(item.job.progress) || 0));
}
function batchPending() {
  if (!currentBatch) return [];
  if (batchRetryOnly && batchSelection)
    return currentBatch.items.filter((item) => item.card_id === batchSelection);
  return currentBatch.items.filter((item) => !item.job);
}
function parameterFields(idPrefix, field, value = "") {
  const id = idPrefix + field.key;
  const label = `${esc(localized(field.label))}${field.required ? " *" : ""}`;
  const control = field.type === "file"
    ? `<input id="${id}" type="file" ${field.required && !value ? "required" : ""}>${value ? `<input id="${id}-retained" type="hidden" value="${esc(value)}"><p class="caption">${tr("已保留上次上传的文件，可重新选择替换。", "Your previous upload is retained. Choose a new file to replace it.")}</p>` : ""}<p class="caption" data-upload-limit>${uploadLimitCaption()}</p>`
    : field.type === "textarea"
      ? `<textarea id="${id}" ${field.required ? "required" : ""} maxlength="10000">${esc(value)}</textarea>`
      : `<input id="${id}" type="${field.type}" value="${esc(value)}" ${field.type === "number" ? 'step="any"' : ""} ${field.required ? "required" : ""} maxlength="10000">`;
  const help = localized(field.description)
    ? `<details ${field.collapsed ? "" : "open"}><summary>${tr("填写说明", "Instructions")}</summary><div class="markdown">${md(localized(field.description))}</div></details>`
    : "";
  return `<div class="field"><label for="${id}">${label}</label>${control}</div>${help}`;
}
function batchItemButton(item) {
  const bits = [];
  if (item.variant?.name) bits.push(esc(item.variant.name));
  bits.push(`${batchProgress(item)}%`);
  if (item.job?.message) bits.push(esc(item.job.message));
  const mark = item.job
    ? status(item.job)
    : `<span class="status">${esc(tr("待填写", "Details needed"))}</span>`;
  return `<button type="button" class="batch-item" data-card="${esc(item.card_id)}"><span class="batch-item-main"><strong>···${esc(item.suffix || "????")}</strong><small>${bits.join(" · ")}</small></span>${mark}</button>`;
}
function openBatchCard(item) {
  selectBatchCard(item.card_id, !item.job);
  currentProduct = batchProduct(item);
  currentJob = item.job || null;
  currentVariant = item.variant || item.job?.variant || null;
  if (!item.job) {
    redemptionForm();
    return;
  }
  renderReceipt(item.job);
}
function bindBatchItems(items) {
  for (const item of items) on(`[data-card="${item.card_id}"]`, () => openBatchCard(item));
}
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
  const limit = await uploadFileLimit();
  if (!file || file.size > limit)
    throw new Error(uploadLimitCaption());
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.append(key, value);
  body.append("file", file, file.name || "attachment");
  return (await fileResponse(path, { method: "POST", body, signal: options.signal })).json();
}
function uploadLimitCaption() {
  const amount = Math.round(maxUploadFileBytes / (1024 * 1024) * 100) / 100;
  return tr(`每个文件最多 ${amount} MiB。`, `Each file can be at most ${amount} MiB.`);
}
function uploadFileLimit() {
  if (!uploadLimitPromise) uploadLimitPromise = api("/upload-limits").then((limits) => {
    if (Number.isSafeInteger(limits.max_file_bytes) && limits.max_file_bytes > 0)
      maxUploadFileBytes = Math.min(maxUploadFileBytes, limits.max_file_bytes);
  }).catch(() => {}).then(() => {
    document.querySelectorAll("[data-upload-limit]").forEach((node) => { node.textContent = uploadLimitCaption(); });
    return maxUploadFileBytes;
  });
  return uploadLimitPromise;
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
  const result = await uploadMultipart(jobScope ? "/manage/files/upload" : "/files/upload", jobScope ? { job_id: definition.job_id, field_key: definition.field_key } : receiptCardBody(context, { field_key: definition.field_key }), file, options);
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
  const response = await fileResponse("/files/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(receiptCardBody(context, { file_id: file.id })) });
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
  currentBatch = null;
  batchSelection = "";
  batchRetryOnly = false;
  currentProduct = null;
  currentJob = null;
  currentVariant = null;
  const list = await api("/products");
  app.innerHTML = `<section class="intro home-paper-stack"><section class="intro-copy public-card" data-home-paper="products" tabindex="0" aria-label="${tr("公开商品", "Public products")}"><h1>${list.length ? tr("公开商品", "Public products") : tr("暂无公开商品", "No public products")}</h1><div class="paper-divider" aria-hidden="true"></div>${list.length ? `<p class="caption">${tr("选择商品了解详情，兑换需有效卡密。", "Browse the details. A valid code is required to redeem.")}</p><div class="home-product-list" tabindex="0" role="region" aria-label="${tr("公开商品列表，可上下滚动", "Public product list, scroll vertically")}">${list.map((p, i) => `<article class="product-row">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${esc(p.name)}</h3><p>${tr(p.delivery === "service" ? "服务兑换" : "商品领取", p.delivery === "service" ? "Service" : "Digital delivery")} · ${tr(p.mode === "manual" ? "队列处理" : "自动处理", p.mode === "manual" ? "Queue processing" : "Automatic")}</p></div><button class="secondary" data-product="${i}">${tr("查看详情", "Details")}</button></article>`).join("")}</div>` : ""}</section><section class="exchange" data-home-paper="redeem" tabindex="0" aria-label="${tr("卡密兑换", "Redeem a code")}"><h2>${tr("开始兑换", "Redeem your code")}</h2><p>${tr("请使用购买商品时获得的卡密。", "Use the code you received with your purchase.")}</p><form id="form"><div class="field"><label for="code">${tr("兑换卡密", "Redemption code")}</label><div class="code-entry"><textarea class="code" id="code" rows="2" placeholder="XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX" required autocomplete="off" spellcheck="false" maxlength="8000"></textarea><button id="paste-code" class="secondary paste-code" type="button">${tr("粘贴", "Paste")}</button></div><p id="code-count" class="caption"></p><p class="caption">${tr("多张卡密请换行，或用空格、逗号分开。验证后仍得到一个领取链接。", "Separate extra codes with new lines, spaces, or commas. You still get one receipt link.")}</p></div><button type="submit" class="full">${tr("验证并继续", "Verify and continue")} →</button><div id="error" class="error" role="alert"></div></form><div class="footnote">${tr("已有领取链接？直接打开即可查看处理进度。", "Already have a receipt link? Open it to check progress.")}</div></section></section>`;
  form(() => {
    const codes = splitCodes($("#code").value);
    if (!codes.length) throw new Error(tr("请输入卡密", "Enter a redemption code"));
    if (codes.length > 30) throw new Error(tr("一次最多兑换 30 张卡密", "You can redeem at most 30 codes at once"));
    return exchangeCode($("#code").value);
  });
  on("#paste-code", pasteCodes);
  $("#code")?.addEventListener("input", updateCodeCount);
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
  const selection = batchSelection;
  const retryOnly = batchRetryOnly;
  return {
    token,
    cardId: currentBatch && selection ? selection : null,
    active: () =>
      receiptGeneration === generation &&
      currentToken === token &&
      batchSelection === selection &&
      batchRetryOnly === retryOnly &&
      location.pathname === pathname &&
      location.hash === hash &&
      !options.signal?.aborted,
  };
}
function openBatch(data) {
  currentBatch = data;
  currentProduct = data.product;
  currentJob = null;
  if (batchSelection) {
    const item = data.items.find((entry) => entry.card_id === batchSelection);
    if (item?.job && !batchRetryOnly) {
      currentProduct = batchProduct(item);
      currentJob = item.job;
      currentVariant = item.variant || item.job.variant || null;
      renderReceipt(item.job);
      return;
    }
    if (item && (batchRetryOnly || !item.job)) {
      currentProduct = batchProduct(item);
      currentJob = item.job || null;
      currentVariant = item.variant || item.job?.variant || null;
      redemptionForm();
      return;
    }
    selectBatchCard();
  }
  selectBatchCard();
  if (data.items.some((item) => !item.job)) redemptionForm();
  else renderBatch(data);
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
    currentJob = data.job || null;
    currentVariant = data.variant || data.job?.variant || data.items?.[0]?.variant || null;
    currentBatch = data.batch ? data : null;
    batchSelection = "";
    batchRetryOnly = false;
    history.pushState({}, "", "/receipt#" + currentToken);
    if (data.batch) openBatch(data);
    else data.job ? renderReceipt(data.job) : redemptionForm();
  };
  if (window.ExtoreMotion && app.querySelector?.(".exchange"))
    await window.ExtoreMotion.transition(app, render, () => context.active() || (!options.signal?.aborted && receiptGeneration === generation && currentToken === data.token && location.pathname === "/receipt" && location.hash === "#" + data.token));
  else render();
  return data;
}
async function submitRedemption(params, options = {}) {
  const context = receiptRequestContext(options);
  const body = Array.isArray(params)
    ? { token: context.token, items: params }
    : { token: context.token, params };
  const result = await api("/redeem", body, "POST", options);
  if (context.active()) {
    selectBatchCard();
    if (result.batch) {
      currentBatch = result;
      openBatch(result);
    } else renderReceipt(result);
  }
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
    currentJob = result.job || null;
    currentVariant = result.variant || result.job?.variant || result.items?.[0]?.variant || null;
    if (result.batch) {
      currentBatch = result;
      openBatch(result);
    } else {
      currentBatch = null;
      batchSelection = "";
      batchRetryOnly = false;
      result.job ? renderReceipt(result.job) : redemptionForm();
    }
  }
  return result;
}
async function revealReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const viewPolicy = currentProduct?.view_policy;
  const fields = currentProduct?.outputs || [];
  const result = await api(
    "/receipt/reveal",
    receiptCardBody(context),
    "POST",
    options,
  );
  if (context.active()) {
    const content = $("#content");
    if (content) {
      const output = result.output || { content: result.content };
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
    receiptCardBody(context),
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
  const pending = currentBatch ? batchPending() : null;
  if (currentBatch && !pending.length) {
    selectBatchCard();
    renderBatch(currentBatch);
    return;
  }
  const fields = pending
    ? pending.map((item, index) => {
        const itemProduct = batchProduct(item);
        const variant = item.variant?.name ? ` · ${esc(item.variant.name)}` : "";
        const copy = index === 0 && pending.length > 1 && itemProduct.parameters.some((field) => field.type !== "file")
          ? `<button type="button" class="secondary" id="copy-params">${tr("填到其余卡密", "Copy to the other codes")}</button>`
          : "";
        const saved = item.job?.state === "needs_input" ? item.job.params || {} : {};
        return `<section class="batch-card"><h3>${tr("卡密", "Code")} ···${esc(item.suffix || "")}${variant}</h3>${item.job?.state === "needs_input" ? `<p class="error">${esc(item.job.message)}</p>` : ""}${copy}${itemProduct.parameters.map((field) => parameterFields(`param-${item.card_id}-`, field, saved[field.key] || "")).join("")}</section>`;
      }).join("")
    : p.parameters.map((field) => parameterFields(`param-`, field, currentJob?.state === "needs_input" ? currentJob.params?.[field.key] || "" : "")).join("");
  const started = currentBatch
    ? currentBatch.items.filter((item) => item.job && !pending.some((row) => row.card_id === item.card_id))
    : [];
  const back = currentBatch && batchSelection
    ? `<button type="button" id="batch-back" class="secondary">← ${tr("全部卡密", "All codes")}</button>`
    : "";
  const countNote = pending
    ? `<p class="caption">${tr(`一次提交 ${pending.length} 张卡密。文件需要分别上传，其余内容可以从第一张复制。`, `Submit ${pending.length} codes together. Upload each file separately; other fields can be copied from the first code.`)}</p>`
    : `<p class="caption">${tr("提交后会创建兑换任务，请保存领取链接。", "Save your receipt link after submitting.")}</p>`;
  app.innerHTML = `<div class="narrow">${back}<h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("填写信息", "Your details")}</div><div class="step">${tr("领取商品", "Collect")}</div></div>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="">` : ""}<div class="markdown">${md(p.description)}</div><div class="panel"><h2>${tr("确认兑换信息", "Confirm your details")}</h2><form id="form">${fields}<button type="submit" class="full">${tr("确认兑换", "Confirm redemption")}</button>${countNote}<div id="error" class="error" role="alert"></div></form>${started.length ? `<div class="batch-started"><h3>${tr("已开始处理", "Already started")}</h3><div class="batch-list">${started.map(batchItemButton).join("")}</div></div>` : ""}</div></div>`;
  on("#batch-back", () => {
    selectBatchCard();
    openBatch(currentBatch);
  });
  on("#copy-params", () => {
    const source = pending[0];
    for (const field of batchProduct(source).parameters) {
      if (field.type === "file") continue;
      const from = document.getElementById(`param-${source.card_id}-${field.key}`);
      if (!from) continue;
      for (const item of pending.slice(1)) {
        if (!batchProduct(item).parameters.some((target) => target.key === field.key && target.type === field.type)) continue;
        const target = document.getElementById(`param-${item.card_id}-${field.key}`);
        if (target) target.value = from.value;
      }
    }
    toast(tr("已填到其余卡密。文件仍需分别上传。", "Copied to the other codes. Files still need their own upload."));
  });
  bindBatchItems(started);
  if ((pending ? pending.some((item) => batchProduct(item).parameters.some((field) => field.type === "file")) : p.parameters.some((field) => field.type === "file"))) void uploadFileLimit();
  form(async () => {
    const context = receiptRequestContext();
    if (!pending) {
      const params = {};
      for (const field of p.parameters) {
        const input = document.getElementById("param-" + field.key);
        if (field.type === "file") {
          const file = input.files[0];
          params[field.key] = file ? (await uploadMultipart("/files/upload", { token: context.token, field_key: field.key }, file)).id : document.getElementById("param-" + field.key + "-retained")?.value || "";
        } else params[field.key] = input.value;
        if (!context.active()) return;
      }
      await submitRedemption(params);
      return;
    }
    const items = [];
    for (const item of pending) {
      const params = {};
      for (const field of batchProduct(item).parameters) {
        const input = document.getElementById(`param-${item.card_id}-${field.key}`);
        if (field.type === "file") {
          const file = input.files[0];
          params[field.key] = file
            ? (await uploadMultipart("/files/upload", { token: context.token, field_key: field.key, card_id: item.card_id }, file)).id
            : document.getElementById(`param-${item.card_id}-${field.key}-retained`)?.value || "";
        } else params[field.key] = input.value;
        if (!context.active()) return;
      }
      items.push({ card_id: item.card_id, params });
    }
    await submitRedemption(items);
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
function pollBatch(data) {
  const waiting = (data.items || []).some((item) => ["queued", "processing"].includes(item.job?.state));
  if (!waiting) return;
  const context = receiptRequestContext();
  timer = setTimeout(async () => {
    if (!context.active() || batchSelection) return;
    try { await readReceipt(); }
    catch (e) {
      if (context.active() && !batchSelection) {
        toast(e.message);
        pollBatch(currentBatch);
      }
    }
  }, document.hidden ? 15000 : 2500);
}
function renderBatch(data) {
  stopPoll();
  window.ExtoreWebMCP?.refresh();
  const items = data.items || [];
  const finished = items.filter((item) => ["succeeded", "failed", "rejected", "destroyed"].includes(item.job?.state)).length;
  const mean = items.length ? Math.round(items.reduce((sum, item) => sum + batchProgress(item), 0) / items.length) : 0;
  const viewKey = JSON.stringify(["batch", currentToken, lang, items.map((item) => [item.card_id, item.suffix, item.job?.state, item.job?.progress, item.job?.message, item.job?.queue_position])]);
  if (receiptViewKey === viewKey && $("#batch-list")) {
    pollBatch(data);
    return;
  }
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = viewKey;
  const p = currentProduct;
  app.innerHTML = `<div class="narrow"><h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("信息已提交", "Details submitted")}</div><div class="step ${finished === items.length ? "active" : ""}">${tr("领取商品", "Collect")}</div></div><section class="panel"><div class="section-head"><h2>${tr("整体进度", "Overall progress")}</h2><span class="caption">${finished} / ${items.length}</span></div><p id="customer-message">${esc(tr(`已完成 ${finished} / ${items.length} 张。点开一张可查看它自己的处理进度。`, `${finished} of ${items.length} finished. Open one code to see its own progress.`))}</p><div class="progress" role="progressbar" aria-label="${tr("整体进度", "Overall progress")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${mean}"><span></span></div><p id="progress-label" class="caption">${mean}%</p><div id="batch-list" class="batch-list">${items.map(batchItemButton).join("")}</div></section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(location.origin + "/receipt#" + currentToken)}</p><button id="copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("这一个链接包含全部卡密。有效期 30 天，请勿转发给他人。", "This one link covers every code. Valid for 30 days. Do not forward it.")}</p></div></div>`;
  const bar = $(".progress span");
  if (bar) bar.style.transform = "scaleX(" + mean / 100 + ")";
  bindBatchItems(items);
  on("#copy", async () => {
    const copied = await writeClipboard(location.origin + "/receipt#" + currentToken);
    toast(copied ? tr("链接已复制", "Link copied") : tr("复制失败，请手动复制上方链接。", "Could not copy. Copy the link above manually."));
  });
  pollBatch(data);
}
function renderReceipt(j) {
  currentJob = j;
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
  const batchItem = currentBatch?.items?.find((item) => item.card_id === batchSelection);
  const batchBack = batchItem ? `<button id="batch-back" class="secondary" type="button">← ${tr("全部卡密", "All codes")}</button>` : "";
  const suffixLine = batchItem ? `<p class="caption">${tr("卡密尾号", "Code ending")} ···${esc(batchItem.suffix || "")}</p>` : "";
  app.innerHTML = `<div class="narrow ${waiting ? "receipt-waiting" : ""}">${batchBack}<h1>${esc(p.name)}</h1>${suffixLine}${variant?.name ? `<p class="caption">${tr("规格：", "Variant: ")}${esc(variant.name)}</p>` : ""}<div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("信息已提交", "Details submitted")}</div><div class="step ${j.state === "succeeded" ? "active" : ""}">${tr("领取商品", "Collect")}</div></div><section class="panel receipt-panel">${waiting ? '<div class="paper-divider waiting-top" aria-hidden="true"></div>' : ""}<div class="section-head"><h2>${tr("兑换进度", "Redemption progress")}</h2>${status(j)}</div><p id="customer-message"></p><p id="queue-position" class="caption"></p><div id="fulfillment-steps"></div>${waiting ? `<div class="progress" role="progressbar" aria-label="${tr("处理进度", "Processing progress")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${j.progress}"><span></span></div><p id="progress-label" class="caption"></p>${window.ExtoreMotion?.waitingMarkup(lang) || ""}<button id="motion-toggle" class="secondary motion-toggle" aria-pressed="${waitingAnimationPaused}">${waitingAnimationPaused ? tr("播放动画", "Play animation") : tr("暂停动画", "Pause animation")}</button>${reminderMarkup(j)}` : ""}<div id="content"></div><div class="actions">${j.state === "succeeded" && j.delivery === "content" ? `<button id="reveal">${tr(j.view_policy === "once" ? "领取内容（仅一次）" : "查看交付内容", j.view_policy === "once" ? "Reveal once" : "View your goods")}</button>` : ""}${j.can_retry ? `<button id="retry">${tr(j.state === "needs_input" ? "补充需求并重新提交" : "重新填写并重试", j.state === "needs_input" ? "Add details and resubmit" : "Update details and retry")}</button>` : ""}${j.state === "succeeded" ? `<button id="destroy" class="danger">${tr("立即销毁", "Destroy now")}</button>` : ""}</div><div id="error" class="error" role="alert"></div>${j.state === "destroyed" ? `<p class="caption">${tr("内容已永久删除，此链接无法再领取。", "The content has been deleted. This link can no longer reveal it.")}</p>` : ""}${waiting ? '<div class="paper-divider waiting-bottom" aria-hidden="true"></div>' : ""}</section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(location.origin + "/receipt#" + currentToken)}</p><button id="copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${batchItem ? tr("这个链接包含全部卡密，有效期 30 天，请勿转发给他人。", "This link covers every code. Valid for 30 days. Do not forward it.") : tr("有效期 30 天。链接是领取凭证，请勿转发给他人。", "Valid for 30 days. Anyone with this link can access the receipt.")}</p></div></div>`;
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
  on("#batch-back", () => {
    selectBatchCard();
    openBatch(currentBatch);
  });
  on("#retry", () => {
    selectBatchCard(batchSelection, Boolean(currentBatch && batchSelection));
    redemptionForm();
  });
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
  managementMaxCLIUses = auth.max_cli_uses ?? 1;
  managementRemainingCLIUses = auth.remaining_cli_uses ?? Math.max(0, managementMaxCLIUses - (auth.cli_uses || 0));
  managementLinkId = auth.link_id || auth.staff_id || null;
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
async function copyCLIPrompt(options, host, isCurrent = () => true) {
  const prompt = window.ExtoreCliPrompts.build({ language: lang, ...options });
  if (!isCurrent()) return;
  host.innerHTML = `<div class="field"><label for="cli-ai-prompt">${tr("给 AI 的 CLI 提示词", "CLI prompt for AI")}</label><textarea id="cli-ai-prompt" readonly spellcheck="false" rows="12">${esc(prompt)}</textarea></div><p class="caption">${tr("包含授权链接时请私下交付，不要公开发布。", "Share authorization prompts privately; do not publish them.")}</p>`;
  const input = $("#cli-ai-prompt");
  const copied = await writeClipboard(prompt);
  if (!isCurrent()) return;
  if (!copied) {
    input.focus();
    input.select();
  }
  toast(copied ? tr("AI 提示词已复制", "AI prompt copied") : tr("复制失败，已选中提示词，请手动复制。", "Could not copy. The prompt is selected; copy it manually."));
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
async function renderJobs(filter = "", requestedProductId = queueProductId, requestedView = queueView) {
  const loadId = ++queueLoadId;
  const pathname = location.pathname;
  const view = ["active", "processed", "all"].includes(requestedView) ? requestedView : "active";
  queueView = view;
  document.querySelectorAll("[name=job]").forEach((node) => (node.checked = false));
  if ($("#all")) $("#all").checked = false;
  if ($("#batch-form")) $("#batch-form").innerHTML = "";
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
  const query = new URLSearchParams({ product_id: productId, view });
  if (filter) query.set("state", filter);
  const rows = await api("/manage/jobs?" + query);
  if (!active()) return;
  const manual = selectedProduct.mode === "manual";
  const canProcess = manual && permitted("queue.process");
  let batchDialogGeneration = 0;
  const dialogCurrent = (generation) => active() && generation === batchDialogGeneration;
  const closeBatchDialog = () => { batchDialogGeneration++; $("#batch-form").innerHTML = ""; };
  $("#workspace").innerHTML = `
    <div class="field queue-picker"><label for="queue-product">选择商品队列</label><select id="queue-product" ${role === "staff" ? "disabled" : ""}>${available.map((p) => `<option value="${esc(p.id)}" ${p.id === productId ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>
    <div class="queue-heading"><h2>${esc(selectedProduct.name)} · 处理队列</h2><button id="copy-queue-ai" class="secondary">复制给 AI 的提示词</button><p class="caption">${manual ? "本队列只处理这个商品。先领取任务，再更新进度或提交结果。" : "本商品由程序自动处理，这里查看进度与处理记录。"}</p>${role === "staff" ? `<p class="caption">CLI 剩余绑定次数：${managementRemainingCLIUses}。${managementRemainingCLIUses ? "复制提示词会生成五分钟有效的 CLI 绑定票据。" : "请使用已绑定的 CLI 设备；不会创建新的授权票据。"}</p>` : ""}</div><div id="queue-ai-prompt"></div>
    <div class="toolbar"><select id="job-view" aria-label="队列视图"><option value="active" ${view === "active" ? "selected" : ""}>待处理</option><option value="processed" ${view === "processed" ? "selected" : ""}>已处理</option><option value="all" ${view === "all" ? "selected" : ""}>全部</option></select><select id="job-state" aria-label="任务状态"><option value="">全部状态</option>${Object.entries(
      states,
    )
      .map(
        ([k, v]) =>
          `<option value="${k}" ${filter === k ? "selected" : ""}>${v}</option>`,
      )
      .join(
        "",
      )}</select><button id="refresh" class="secondary">刷新</button>${canProcess ? '<button id="claim">领取选中任务</button><button id="progress-update" class="secondary">更新进度</button><button id="complete" class="secondary">批量完成</button><button id="fail" class="secondary">标记失败</button><button id="request-changes" class="secondary">要求补充信息</button><button id="reject" class="danger">永久拒绝</button>' : ""}${role === "admin" ? '<button id="release" class="secondary">核实后允许重试</button>' : ""}</div>
    ${manual ? '<p class="caption">批量操作只作用于当前商品，全部成功才提交。批量完成会给所选任务相同的交付内容。</p>' : ""}
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th><input id="all" type="checkbox" aria-label="选择当前商品的全部任务"></th><th>任务</th><th>用户参数</th><th>状态 / 进度</th><th>尝试</th></tr></thead><tbody>${rows.map((j) => `<tr><td><input type="checkbox" name="job" value="${esc(j.id)}" aria-label="选择 ${esc(j.id)}"></td><td class="mono">${esc(j.id)}${j.variant?.name ? `<p class="caption">${esc(j.variant.name)}</p>` : ""}${j.queue_position ? `<p class="caption">队列第 ${j.queue_position} 位</p>` : ""}</td><td><pre>${esc(JSON.stringify(j.params, null, 2))}</pre>${(j.files || []).filter((file) => file.kind === "input").map((file) => `<p><a href="/api/manage/files/${encodeURIComponent(file.id)}/download">↓ ${esc(file.filename)}</a> <span class="caption">${Math.ceil(file.size / 1024)} KiB</span></p>`).join("")}</td><td>${status(j)}<p>${j.progress}% · ${esc(j.message)}</p>${j.steps?.length ? `<p class="caption">${j.steps.filter((step) => step.done).length} / ${j.steps.length} 步已完成</p>` : ""}</td><td>${j.attempt}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">这个商品暂无符合条件的任务。</div>'}
    <div id="batch-form"></div><div id="error" class="error" role="alert"></div>`;
  window.ExtoreWebMCP?.refresh();
  on("#copy-queue-ai", async () => {
    if (!active()) return;
    let authorization = { origin: location.origin };
    if (role === "staff" && managementRemainingCLIUses > 0) {
      const ticket = await api("/manage/cli-ticket", {});
      if (!active()) return;
      if (ticket.product_id !== productId) throw new Error("授权票据不属于当前商品。");
      authorization = { origin: ticket.origin, link: ticket.origin + "/cli#" + ticket.token, expires: ticket.expires };
    }
    await copyCLIPrompt({ ...authorization, product: selectedProduct, permissions: role === "staff" ? permissions : [], reuseDevice: role === "staff" && managementRemainingCLIUses === 0 }, $("#queue-ai-prompt"), active);
  });
  $("#queue-product").addEventListener("change", () =>
    perform(() => renderJobs("", $("#queue-product").value, view)),
  );
  $("#job-view").addEventListener("change", () =>
    perform(() => renderJobs(filter, productId, $("#job-view").value)),
  );
  $("#job-state").addEventListener("change", () =>
    perform(() => renderJobs($("#job-state").value, productId, view)),
  );
  on("#refresh", () => renderJobs(filter, productId, view));
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
  const executeBatch = (body) => {
    if (!active()) throw new Error("商品队列已切换，请重新选择任务。");
    return api("/manage/batch", { product_id: productId, ...body });
  };
  const refreshQueue = () => active() ? renderJobs(filter, productId, view) : undefined;
  on("#claim", async () => {
    await executeBatch({ ids: ids(), action: "claim" });
    await refreshQueue();
  });
  on("#release", async () => {
    if (!confirm("确认已核实外部平台没有交付？允许重试可能再次调用发货程序。"))
      return;
    await executeBatch({ ids: ids(), action: "retry" });
    await refreshQueue();
  });
  function disposition(action) {
    const selected = ids();
    const actor = role === "admin" ? "owner" : managementLinkId;
    const selectedRows = rows.filter((row) => selected.includes(row.id));
    if (!canProcess || selectedRows.length !== selected.length || selectedRows.some((row) => row.state !== "processing" || row.claimed_by !== actor))
      throw new Error("只能处理你已领取的处理中任务。");
    const generation = ++batchDialogGeneration;
    const rejecting = action === "reject";
    $("#batch-form").innerHTML = `<div class="panel"><h2>${rejecting ? "永久拒绝" : "要求补充信息"} · ${selected.length} 个任务</h2><div class="field"><label for="disposition-reason">${rejecting ? "拒绝原因" : "需要顾客补充的内容"} *</label><textarea id="disposition-reason" required maxlength="1000"></textarea></div><p class="caption">${rejecting ? "顾客将看到原因，这张卡密不能再次提交。" : "顾客将看到原因，并可保留已有信息后重新提交。"}</p><div class="actions"><button id="disposition-submit" class="${rejecting ? "danger" : ""}">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", closeBatchDialog);
    on("#disposition-submit", async () => {
      if (!dialogCurrent(generation)) return;
      const message = $("#disposition-reason").value.trim();
      if (!message || message.length > 1000) throw new Error("请填写原因，最多 1000 字。");
      if (rejecting && !confirm("永久拒绝这些任务并让卡密无法再次提交？顾客会看到拒绝原因。")) return;
      await executeBatch({ ids: selected, action, message });
      if (dialogCurrent(generation)) await refreshQueue();
    });
  }
  on("#request-changes", () => disposition("request_changes"));
  on("#reject", () => disposition("reject"));
  async function finish(action) {
    const generation = ++batchDialogGeneration;
    const selected = ids();
    let selectedRows = rows.filter((row) => selected.includes(row.id));
    if (selectedRows.length !== selected.length) throw new Error("任务不属于当前显示的商品队列，请刷新后重新选择。");
    let outputFields = selectedRows[0]?.outputs || selectedProduct.outputs || [];
    if (action === "succeed") {
      if (selectedRows.some((row) => JSON.stringify(row.outputs || selectedProduct.outputs || []) !== JSON.stringify(outputFields)))
        throw new Error("所选任务的交付定义不同，请分别交付。");
      if (selected.length > 1 && outputFields.some((field) => field.type === "file"))
        throw new Error("包含交付文件的任务请逐个完成，文件只属于自己的任务。");
      if (outputFields.some((field) => field.type === "file")) {
        const query = new URLSearchParams({ product_id: productId, job_id: selected[0], limit: "1" });
        const fresh = await api("/manage/jobs?" + query);
        if (!dialogCurrent(generation)) return;
        const current = fresh.find((row) => row.id === selected[0] && row.product_id === productId);
        if (!current) throw new Error("任务不属于当前商品队列。");
        selectedRows = [current];
        outputFields = current.outputs || selectedProduct.outputs || [];
      }
    }
    if (!dialogCurrent(generation)) return;
    const uploadedOutputs = (field) => (selectedRows[0]?.files || []).filter((file) =>
      file.job_id === selected[0] && file.kind === "output" && file.field_key === field.key &&
      !file.consumed && file.available !== false && (file.attempt == null || file.attempt === selectedRows[0].attempt));
    const fileField = (field, index) => {
      const attachments = uploadedOutputs(field);
      return `${attachments.length ? `<label for="batch-uploaded-${index}">已上传附件</label><select id="batch-uploaded-${index}"><option value="">选择已上传附件，或选择新文件</option>${attachments.map((file) => `<option value="${esc(file.id)}">${esc(file.filename)} · ${Math.ceil(file.size / 1024)} KiB</option>`).join("")}</select><p id="batch-uploaded-id-${index}" class="caption mono"></p>${attachments.map((file) => `<p class="caption"><a href="/api/manage/files/${encodeURIComponent(file.id)}/download">检查附件：${esc(file.filename)}</a> · ${esc(file.id)}</p>`).join("")}` : ""}<input id="batch-output-${index}" type="file" ${field.required ? "required" : ""}><p class="caption" data-upload-limit>${uploadLimitCaption()}</p><p class="caption">文件只交付给此任务。选择新文件将替代已上传附件。</p>`;
    };
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>${action === "succeed" ? "批量完成" : "标记失败"} · ${esc(selectedProduct.name)} · ${selected.length} 个任务</h2>${textarea("batch-message", "处理说明")}${action === "succeed" ? outputFields.map((output, i) => `<div class="field"><label for="batch-output-${i}">${esc(localized(output.label))}${output.required ? " *" : ""}</label>${output.type === "file" ? fileField(output, i) : output.type === "textarea" ? `<textarea id="batch-output-${i}" maxlength="100000" ${output.required ? "required" : ""}></textarea>` : `<input id="batch-output-${i}" type="${output.type}" ${output.type === "number" ? 'step="any"' : ""} maxlength="100000" ${output.required ? "required" : ""}>`}</div>${localized(output.description) ? `<details ${output.collapsed ? "" : "open"}><summary>交付说明</summary><div class="markdown">${md(localized(output.description))}</div></details>` : ""}`).join("") || '<p class="caption">此商品只交付服务状态，无需填写内容。</p>' : '<div class="checks"><label><input id="batch-retry" type="checkbox">已确认未交付，允许顾客重试</label></div>'}<div class="actions"><button id="batch-submit">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", closeBatchDialog);
    for (const [index, field] of outputFields.entries()) {
      if (action !== "succeed" || field.type !== "file") continue;
      const input = $("#batch-output-" + index);
      const existing = $("#batch-uploaded-" + index);
      const updateSelection = () => {
        input.required = !!field.required && !existing?.value;
        const label = $("#batch-uploaded-id-" + index);
        if (label) label.textContent = existing?.value ? "已选择附件 ID：" + existing.value : "";
      };
      existing?.addEventListener("change", updateSelection);
      input.addEventListener("change", updateSelection);
      updateSelection();
    }
    if (action === "succeed" && outputFields.some((field) => field.type === "file")) void uploadFileLimit();
    on("#batch-submit", async () => {
      if (!dialogCurrent(generation)) return;
      const output = {};
      const message = $("#batch-message").value;
      if (action === "succeed") {
        for (const [i, definition] of outputFields.entries()) {
          const input = $("#batch-output-" + i);
          if (!input.reportValidity()) return;
          if (definition.type === "file") {
            const file = input.files[0];
            const existing = $("#batch-uploaded-" + i)?.value || "";
            if (existing && !uploadedOutputs(definition).some((candidate) => candidate.id === existing))
              throw new Error("附件不属于此任务、字段或当前尝试，请刷新后重新选择。");
            output[definition.key] = file ? (await uploadMultipart("/manage/files/upload", { job_id: selected[0], field_key: definition.key }, file)).id : existing;
          } else output[definition.key] = input.value;
          if (!dialogCurrent(generation)) return;
        }
      }
      await executeBatch({
        ids: selected,
        action,
        message,
        output: action === "succeed" ? output : undefined,
        retryable: $("#batch-retry")?.checked || false,
      });
      if (dialogCurrent(generation)) await refreshQueue();
    });
  }
  on("#progress-update", () => {
    const generation = ++batchDialogGeneration;
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
    on("#batch-cancel", closeBatchDialog);
    on("#batch-submit", async () => {
      if (!dialogCurrent(generation)) return;
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
      if (dialogCurrent(generation)) await refreshQueue();
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
    `<h2>商品管理链接</h2><p class="caption">一条链接只授权一个商品。可以分别授予队列、商品配置、卡密等权限；店长可获得该商品的完整管理权限。默认可绑定浏览器一次、CLI 一次；已登录的会话可继续使用。链接是登录凭证，请私下交给管理者。</p>${role === "staff" ? '<p class="caption">你只能创建权限比自己更小的下级链接，有效期也不能超过自己的链接。</p>' : ""}<form id="form"><div class="grid">${field("staff-name", "管理者或链接名称")}<div class="field"><label for="staff-product">授权商品</label><select id="staff-product" ${role === "staff" ? "disabled" : ""}>${productOptions()}</select></div>${field("staff-days", "有效天数", defaultDays, "number")}${field("staff-max-uses", "浏览器绑定次数", 1, "number")}${field("staff-max-cli-uses", "CLI 绑定次数", 1, "number")}</div><fieldset class="permission-fields"><legend>权限范围</legend><div class="permission-presets"><button type="button" id="preset-view" class="secondary">只看队列</button><button type="button" id="preset-process" class="secondary">处理任务</button>${role === "admin" ? '<button type="button" id="preset-manager" class="secondary">店长：完全管理商品</button>' : ""}</div><div class="permission-grid">${availablePermissions.map((key) => `<label><input type="checkbox" name="link-permission" value="${key}" ${["queue.view", "queue.process"].includes(key) ? "checked" : ""}>${permissionLabels[key]}</label>`).join("")}</div><p class="caption">“创建下级管理链接”允许继续委派。下级必须少至少一项权限，不能扩大商品范围或有效期。撤销上级链接会同时撤销全部下级。</p></fieldset><button type="submit" class="full" ${products.length ? "" : "disabled"}>创建管理链接</button><div id="error" class="error" role="alert"></div></form><div id="staff-link"></div><div class="form-divider table-wrap"><table><thead><tr><th>管理链接</th><th>商品</th><th>权限</th><th>有效期</th><th>登录次数</th><th></th></tr></thead><tbody>${links.map((link) => `<tr><td>${esc(link.name)}${link.parent_id ? '<div class="caption">下级链接</div>' : ""}</td><td>${esc(products.find((p) => p.id === link.product_id)?.name || link.product_id)}</td><td>${(link.permissions || []).map((key) => esc(permissionLabels[key] || key)).join("<br>")}</td><td>${link.revoked ? "已撤销" : new Date(link.expires * 1000).toLocaleString()}</td><td>浏览器 ${link.uses || 0} / ${link.max_uses || 1}<p class="caption">剩余 ${link.remaining_uses ?? Math.max(0, (link.max_uses || 1) - (link.uses || 0))} 次</p>CLI ${link.cli_uses || 0} / ${link.max_cli_uses || 1}<p class="caption">剩余 ${link.remaining_cli_uses ?? Math.max(0, (link.max_cli_uses || 1) - (link.cli_uses || 0))} 次</p></td><td>${!link.revoked ? `<button data-revoke="${esc(link.id)}" class="danger">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  $("#staff-max-uses").required = true;
  $("#staff-max-uses").min = "1";
  $("#staff-max-uses").max = String(role === "staff" ? managementMaxUses : 1000);
  $("#staff-max-uses").step = "1";
  $("#staff-max-cli-uses").required = true;
  $("#staff-max-cli-uses").min = "1";
  $("#staff-max-cli-uses").max = String(role === "staff" ? managementMaxCLIUses : 1000);
  $("#staff-max-cli-uses").step = "1";
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
    const productDefinition = products.find((p) => p.id === $("#staff-product").value);
    const result = await api(endpoint, {
      name: $("#staff-name").value,
      product_id: $("#staff-product").value,
      days: +$("#staff-days").value,
      max_uses: Number($("#staff-max-uses").value),
      max_cli_uses: Number($("#staff-max-cli-uses").value),
      permissions: selected,
    });
    if (!active()) return;
    await renderStaff();
    if (!active()) return;
    $("#staff-link").innerHTML =
      `<div class="field"><label for="created-management-link">${tr("商品管理链接", "Product management link")}</label><input id="created-management-link" type="text" value="${esc(result.url)}" readonly autocomplete="off" spellcheck="false"></div><button id="copy-management-link" type="button" class="secondary">${tr("复制链接", "Copy link")}</button><button id="copy-link-ai" type="button" class="secondary">${tr("复制给 AI 的提示词", "Copy prompt for AI")}</button><p class="caption">${tr("链接只显示一次。请现在保存并私下交付。", "This link is shown once. Save it now and share it privately.")}</p><div id="link-ai-prompt"></div>`;
    const input = $("#created-management-link");
    on("#copy-management-link", () =>
      copyManagementLink(result.url, input, () => active() && input.isConnected),
    );
    on("#copy-link-ai", () => copyCLIPrompt({ origin: new URL(result.url).origin, link: result.url, product: productDefinition, permissions: selected, expires: result.expires }, $("#link-ai-prompt"), () => active() && input.isConnected));
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
  const pathname = location.pathname;
  const active = () => generation === queueLoadId && role === viewRole && tab === "sessions" && location.pathname === pathname;
  const prefix = role === "admin" ? "/admin" : "/manage";
  const [sessions, entries, devices] = await Promise.all([api(prefix + "/sessions"), api(prefix + "/audit?limit=200"), api(prefix + "/cli-devices")]);
  if (!active()) return;
  const date = (value) => value ? new Date(value * 1000).toLocaleString() : "—";
  const deviceMarkup = `<h2 class="form-divider">CLI 设备</h2><p class="caption">撤销设备会结束它的 CLI 会话，并阻止它再次申请会话；浏览器会话不受影响。已用绑定次数不会退还。</p>${devices.length ? `<div class="table-wrap"><table><thead><tr><th>设备 / 指纹</th><th>商品 / 管理链接</th><th>绑定 / 最近使用</th><th>状态</th><th></th></tr></thead><tbody>${devices.map((device) => `<tr><td>${esc(device.client_name || "未命名 CLI 设备")}<p class="caption mono">${esc(device.fingerprint || "—")}</p><p class="caption mono">${esc(device.id)}</p></td><td>${esc(device.product_name || device.product_id)}<p class="caption">${esc(device.link_name || "商品管理链接")}</p></td><td>${date(device.created)}<p class="caption">${date(device.last_seen)}</p></td><td>${device.revoked ? "已撤销" : device.active ? "有效" : "授权已失效"}</td><td>${!device.revoked ? `<button class="danger" data-revoke-device="${esc(device.id)}">撤销设备</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : '<p class="caption">暂无 CLI 设备。</p>'}`;
  $("#workspace").innerHTML = `<div class="section-head"><div><h2>会话管理</h2><p class="caption">${role === "admin" ? "查看全店会话，按需注销单个设备。" : permitted("links.delegate") ? "查看自己的会话和下级链接的会话，只限授权商品。" : "查看与注销自己的会话。"} 链接次数耗尽不会结束已有会话。</p></div><button id="sessions-refresh" class="secondary">刷新</button></div><div class="table-wrap"><table><thead><tr><th>登录来源</th><th>设备 / 地址</th><th>登录 / 最近活动</th><th>到期</th><th>状态</th><th></th></tr></thead><tbody>${sessions.map((session) => `<tr><td>${esc(session.link_name || (session.role === "admin" ? "商家 Passkey" : session.role === "bootstrap" ? "首次登录" : "商品管理链接"))}${session.product_name ? `<p class="caption">${esc(session.product_name)}</p>` : ""}<p class="caption">${session.channel === "cli" ? "CLI" : "浏览器"}${session.client_name ? " · " + esc(session.client_name) : ""}</p>${session.current ? '<p class="caption">当前会话</p>' : ""}</td><td class="session-client">${esc(session.ua || "未记录设备")}<p class="caption mono">${esc(session.ip || "—")}</p>${session.device_id ? `<p class="caption mono">设备 ${esc(session.device_id)}</p>` : ""}${session.fingerprint ? `<p class="caption mono">${esc(session.fingerprint)}</p>` : ""}</td><td>${date(session.created)}<p class="caption">${date(session.last_seen)}</p></td><td>${date(session.expires)}</td><td>${session.revoked ? "已注销" : session.active ? "有效" : "已到期"}</td><td>${session.active ? `<button class="danger" data-end-session="${esc(session.id)}">${session.current ? "退出此会话" : "注销"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>${deviceMarkup}<h2 class="form-divider">访问审计</h2><p class="caption">最近 200 条登录、链接使用、委派和注销记录。记录中不包含卡密或授权凭证。</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>目标</th></tr></thead><tbody>${entries.map((entry) => `<tr><td>${date(entry.created)}</td><td>${esc(entry.action)}${entry.channel ? `<p class="caption">${entry.channel === "cli" ? "CLI" : "浏览器"}${entry.client_name ? " · " + esc(entry.client_name) : ""}</p>` : ""}${entry.fingerprint ? `<p class="caption mono">${esc(entry.fingerprint)}</p>` : ""}</td><td class="mono">${esc(entry.actor)}</td><td class="mono">${esc(entry.target)}</td></tr>`).join("")}</tbody></table></div><div id="error" class="error" role="alert"></div>`;
  on("#sessions-refresh", renderSessions);
  document.querySelectorAll("[data-revoke-device]").forEach((button) => button.addEventListener("click", () => perform(async () => {
    if (!active()) return;
    const device = devices.find((item) => item.id === button.dataset.revokeDevice);
    if (!device || device.revoked) return;
    if (!confirm("撤销这个 CLI 设备并结束它的全部 CLI 会话？绑定次数不会退还。")) return;
    await api(prefix + "/cli-devices/" + encodeURIComponent(device.id), undefined, "DELETE");
    if (active()) await renderSessions();
  }, button)));
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
  receiptGeneration++;
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
    queueView,
    batch: Boolean(currentBatch),
    cardId: batchSelection || "",
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
