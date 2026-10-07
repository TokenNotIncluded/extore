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
  currentCardAttributes = {},
  currentEntitlements = null,
  receiptViewKey = "",
  receiptMotion = null,
  receiptRevisionRequest = null,
  deliveryRevealGeneration = 0,
  ownerCliApproval = null,
  deviceCliApproval = null,
  pipelineAuthView = null,
  proxyConfigView = null,
  progressBoardController = null,
  progressBoardShopId = "",
  progressBoardProductId = "",
  progressBoardView = "active",
  accountView = null,
  authStatus = {},
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
  productsView = "active",
  managedProductDeleted = false,
  managedProductPurged = false,
  cardProductId = "",
  queueProductId = "",
  queueView = "active",
  queueProduct = null,
  queueLoadId = 0,
  routeLoadId = 0,
  linkLoadId = 0,
  linkView = "active",
  eventLoadId = 0;
const deliveryBlobs = new Map();
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
  waiting: "等待顾客",
  queued: "排队中",
  processing: "处理中",
  succeeded: "已完成",
  failed: "未完成",
  needs_input: "需要重试",
  rejected: "已拒绝",
  destroyed: "已销毁",
};
const stateEn = {
  waiting: "Waiting for customer",
  queued: "Queued",
  processing: "Processing",
  succeeded: "Completed",
  failed: "Failed",
  needs_input: "Retry needed",
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
function acceptReceiptCardInfo(value) {
  currentCardAttributes = value?.card_attributes || value?.job?.card_attributes || {};
  currentEntitlements = value?.entitlements || value?.job?.entitlements || null;
}
function frozenCardInfo(attributes, entitlement, showAllowance = true) {
  const entries = Object.entries(attributes || {}).filter(([key, value]) => key !== entitlement?.attribute_key &&
    (value === null || ["string", "number", "boolean"].includes(typeof value))).slice(0, 20);
  const allowance = showAllowance && entitlement
    ? `<p class="caption code-entitlement">${esc(localized(entitlement.label) || tr("修改额度", "Revision allowance"))} · ${esc(tr(`剩余 ${entitlement.remaining} / ${entitlement.total}`, `${entitlement.remaining} of ${entitlement.total} remaining`))}</p>` : "";
  return allowance + (entries.length ? `<details class="receipt-code-attributes"><summary>${tr("卡密绑定属性", "Frozen code attributes")}</summary><dl>${entries.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value === null ? "—" : typeof value === "boolean" ? value ? tr("是", "Yes") : tr("否", "No") : value)}</dd>`).join("")}</dl></details>` : "");
}
function selectBatchCard(cardId = "", retryOnly = false) {
  if (batchSelection !== cardId || batchRetryOnly !== retryOnly) { receiptGeneration++; deliveryRevealGeneration++; receiptRevisionRequest = null; clearDeliveryBlobs(); }
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
  return currentBatch.items.filter((item) => !item.job && item.card_id && item.accepted !== false);
}
function batchRedemptionOptions() {
  return {
    app, $, tr, esc, localized, parameterFields, on, form, api,
    token: currentToken,
    receiptURL: location.origin + "/receipt#" + currentToken,
    context: receiptRequestContext,
    uploadMultipart, uploadFileLimit, fieldFiles: selectedFieldFiles,
    submit: (items, errors) => submitRedemption(items, { batchUploadErrors: errors }),
    open: openBatchCard,
    refresh: readReceipt,
    edit: () => { selectBatchCard(); redemptionForm(); },
    overview: () => { selectBatchCard(); renderBatch(currentBatch); },
    home,
    copy: async () => {
      const copied = await writeClipboard(location.origin + "/receipt#" + currentToken);
      toast(copied ? tr("链接已复制", "Link copied") : tr("复制失败，请手动复制上方链接。", "Could not copy. Copy the link above manually."));
    },
  };
}
function isAttachmentField(field) { return ["file", "image", "images"].includes(field.type); }
function fieldAttachmentIds(field, value) {
  if (!value) return [];
  if (field.type !== "images") return [value];
  const ids = JSON.parse(value);
  if (!Array.isArray(ids) || ids.length > (field.max_items || 10) || ids.some((id) => typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) || new Set(ids).size !== ids.length)
    throw new Error(tr("图片集合的附件无效，请重新选择。", "The image attachments are invalid. Select them again."));
  return ids;
}
function richFieldControl(id, field, value = "", { maximum = 10000 } = {}) {
  if (isAttachmentField(field)) {
    const retained = fieldAttachmentIds(field, value);
    return `<input id="${esc(id)}" type="file" ${field.type === "images" ? "multiple" : ""} ${field.type === "file" ? "" : 'accept="image/png,image/jpeg,image/webp"'} ${field.required && !retained.length ? "required" : ""}>${retained.length ? `<input id="${esc(id)}-retained" type="hidden" value="${esc(value)}"><p class="caption">${tr(`已保留 ${retained.length} 个附件，可重新选择替换。`, `${retained.length} attachments retained. Select new files to replace them.`)}</p>` : ""}<p class="caption" data-upload-limit>${uploadLimitCaption()}</p>${field.type === "images" ? `<p class="caption">${tr(`最多 ${field.max_items || 10} 张图片。支持 PNG、JPEG、WebP。`, `Up to ${field.max_items || 10} images. PNG, JPEG and WebP are supported.`)}</p>` : ""}`;
  }
  if (field.type === "select" || field.type === "boolean") {
    const options = field.type === "boolean" ? [{ value: "true", label: { "zh-CN": "是", en: "Yes" } }, { value: "false", label: { "zh-CN": "否", en: "No" } }] : field.options || [];
    return `<select id="${esc(id)}" ${field.required ? "required" : ""}><option value="">${field.required ? tr("请选择", "Choose an option") : tr("未填写", "Not specified")}</option>${options.map((option) => `<option value="${esc(option.value)}" ${option.value === value ? "selected" : ""}>${esc(localized(option.label))}</option>`).join("")}</select>`;
  }
  if (field.type === "textarea") return `<textarea id="${esc(id)}" ${field.required ? "required" : ""} maxlength="${maximum}">${esc(value)}</textarea>`;
  return `<input id="${esc(id)}" type="${field.sensitive ? "password" : esc(field.type)}" value="${esc(field.sensitive ? "" : value)}" ${field.type === "number" ? 'step="any"' : ""} ${field.sensitive ? 'autocomplete="off"' : ""} ${field.required ? "required" : ""} maxlength="${maximum}">`;
}
function selectedFieldFiles(input, field) {
  const files = Array.from(input.files || []);
  if (files.length > (field.type === "images" ? field.max_items || 10 : 1))
    throw new Error(tr("选择的附件数量超过字段限制。", "Too many attachments selected for this field."));
  return files;
}
async function uploadFieldFiles(input, field, retained, url, body, options = {}) {
  const selected = selectedFieldFiles(input, field);
  if (!selected.length) return field.type === "images" ? JSON.stringify(fieldAttachmentIds(field, retained)) : retained || "";
  const ids = [];
  const { isCurrent = () => true, ...requestOptions } = options;
  for (const file of selected) {
    if (!isCurrent()) throw new Error(tr("页面已切换，附件尚未提交。", "The page changed. Attachments were not submitted."));
    ids.push((await uploadMultipart(url, { ...body, field_key: field.key }, file, { ...requestOptions, isCurrent })).id);
    if (!isCurrent()) throw new Error(tr("页面已切换，附件尚未提交。", "The page changed. Attachments were not submitted."));
  }
  return field.type === "images" ? JSON.stringify(ids) : ids[0];
}
window.ExtoreFields = { isAttachment: isAttachmentField, attachmentIds: fieldAttachmentIds, control: richFieldControl, read: (control) => control.value, files: selectedFieldFiles, upload: uploadFieldFiles, imageAccept: "image/png,image/jpeg,image/webp" };
function parameterFields(idPrefix, field, value = "") {
  const id = idPrefix + field.key;
  const label = `${esc(localized(field.label))}${field.required ? " *" : ""}`;
  const control = richFieldControl(id, field, value);
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
  acceptReceiptCardInfo(item);
  if (!item.job) {
    if (currentBatch?.partial && window.ExtoreBatchRedemption?.flowOf(item)?.enabled) {
      stopPoll();
      receiptMotion?.dispose();
      receiptMotion = null;
      receiptViewKey = "";
      window.ExtoreBatchRedemption.renderForm({ ...currentBatch, items: [item] }, batchRedemptionOptions());
      window.ExtoreWebMCP?.refresh();
      return;
    }
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
function managementOptions(options = {}) {
  return { ...options,
    expectedScope: options.expectedScope !== undefined ? options.expectedScope : authStatus.shop_id || (authStatus.superadmin === true ? "platform" : undefined),
    expectedSessionId: options.expectedSessionId ?? options.sessionId ?? authStatus.session_id,
  };
}
function managementAuthority(auth = authStatus) {
  return JSON.stringify([auth.role, auth.shop_id, auth.superadmin === true, auth.link_id || auth.staff_id || null]);
}
function managementHeaders(path, options = {}, headers = {}) {
  const bound = /^\/(?:admin|manage|platform|shop)(?:\/|\?|$)/.test(path) || /^\/auth\/(?:passkeys|register\/(?:options|verify)|password\/change|totp|reauth\/password|logout)(?:\/|\?|$)/.test(path);
  if (bound) {
    const { expectedScope: scope, expectedSessionId: sessionId } = managementOptions(options);
    if (typeof scope === "string") headers["X-Extore-Shop-Scope"] = scope;
    if (typeof sessionId === "string") headers["X-Extore-Session-ID"] = sessionId;
  }
  return headers;
}
async function api(path, body, method, options = {}) {
  const headers = managementHeaders(path, options, body ? { "Content-Type": "application/json" } : {});
  const response = await fetch("/api" + path, {
    method: method || (body ? "POST" : "GET"),
    headers,
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
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : "输入格式有误，请检查表单",
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
async function fileResponse(path, options = {}) {
  const { expectedScope, expectedSessionId, sessionId, ...fetchOptions } = options;
  const response = await fetch("/api" + path, { ...fetchOptions, headers: managementHeaders(path, options, { ...options.headers }) });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : tr("文件操作失败", "File operation failed"));
  }
  return response;
}
async function uploadMultipart(path, fields, file, options = {}) {
  const { isCurrent = () => true, ...rest } = options;
  const requestOptions = managementOptions(rest);
  const limit = await uploadFileLimit();
  if (!isCurrent()) throw new Error(tr("页面已切换，附件尚未提交。", "The page changed. Attachments were not submitted."));
  if (!file || file.size > limit)
    throw new Error(uploadLimitCaption());
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.append(key, value);
  body.append("file", file, file.name || "attachment");
  return (await fileResponse(path, { ...requestOptions, method: "POST", body })).json();
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
function currentManagementActor() { return role === "staff" ? managementLinkId : authStatus.actor || authStatus.account_id || (authStatus.shop_id ? "shop:" + authStatus.shop_id : "owner"); }
function managementFlowScope(job) {
  if (!job.task_flow) return null;
  if (!Number.isSafeInteger(job.flow_epoch) || job.flow_epoch < 1 || !Number.isSafeInteger(job.attempt) || job.attempt < 1 || typeof job.action_id !== "string" || !job.action_id || job.action_id.length > 100)
    throw new Error("当前任务没有可处理的流程步骤，请刷新队列。");
  return { flow_epoch: job.flow_epoch, action_id: job.action_id, attempt: job.attempt };
}
function sameManagementStep(first, next) {
  return JSON.stringify(managementFlowScope(first)) === JSON.stringify(managementFlowScope(next)) && first.attempt === next.attempt;
}
function customerFlow() { return currentJob?.task_flow || currentProduct?.task_flow_view || null; }
function customerStepKey(flow) { return JSON.stringify([flow?.flow_epoch, flow?.revision, flow?.phase, flow?.current?.id]); }
async function selectedJobForFile(definition, options = {}) {
  if (tab !== "jobs" || queueProductId !== definition.product_id || !permitted("queue.view"))
    throw new Error("请先打开对应的商品队列。");
  const query = new URLSearchParams({ product_id: definition.product_id, job_id: definition.job_id, limit: "1" });
  const jobs = await api("/manage/jobs?" + query, undefined, "GET", options);
  const selected = jobs.find((job) => job.id === definition.job_id && job.product_id === definition.product_id);
  if (!selected) throw new Error("任务不属于当前商品队列。");
  return selected;
}
async function uploadFile(definition, options = {}) {
  options = managementOptions(options);
  const context = receiptRequestContext(options);
  const loadId = queueLoadId;
  const productId = queueProductId;
  const authority = managementAuthority(), session = authStatus.session_id;
  const jobScope = definition.scope === "job";
  const initialFlow = customerFlow(), initialStep = customerStepKey(initialFlow);
  const active = () => !options.signal?.aborted && (jobScope ? loadId === queueLoadId && queueProductId === productId && tab === "jobs" && authority === managementAuthority() && session === authStatus.session_id : context.active() && initialStep === customerStepKey(customerFlow()));
  if (!active()) throw new Error("文件操作上下文已失效。");
  let targetJob, uploadFields = { field_key: definition.field_key };
  if (jobScope) {
    if (!permitted("queue.process")) throw new Error("无权上传交付文件。");
    targetJob = await selectedJobForFile(definition, options);
    if (!active()) throw new Error("文件操作上下文已失效。");
    if (!targetJob.outputs?.some((field) => field.key === definition.field_key && isAttachmentField(field))) throw new Error("当前处理步骤没有这个附件输出字段。");
    const scope = managementFlowScope(targetJob);
    if (targetJob.revision?.current > 0 && definition.attempt === undefined)
      throw new Error("修改任务上传必须指定已读取任务的 attempt，请刷新后重新选择。");
    if (definition.attempt !== undefined && definition.attempt !== targetJob.attempt)
      throw new Error("任务尝试已改变，请刷新后重新上传。");
    if (!Number.isSafeInteger(targetJob.attempt) || targetJob.attempt < 1)
      throw new Error("任务尝试信息无效，请刷新队列。");
    uploadFields.attempt = definition.attempt ?? targetJob.attempt;
    if (scope) {
      if (definition.flow_epoch !== scope.flow_epoch || definition.action_id !== scope.action_id || definition.attempt !== scope.attempt) throw new Error("处理步骤已改变，请刷新后重新上传。");
      uploadFields = { ...uploadFields, flow_epoch: scope.flow_epoch, action_id: scope.action_id };
    }
    uploadFields.job_id = definition.job_id;
  } else if (definition.scope !== "customer" || !currentToken || location.pathname !== "/receipt") {
    throw new Error("请先验证卡密。");
  } else {
    const fields = initialFlow ? initialFlow.current?.fields || [] : currentProduct?.parameters || [];
    if (!fields.some((field) => field.key === definition.field_key && isAttachmentField(field))) throw new Error("当前步骤没有这个附件输入字段。");
    if (initialFlow) {
      if (initialFlow.phase !== "input" || definition.flow_epoch !== initialFlow.flow_epoch || definition.expected_revision !== initialFlow.revision || definition.node_id !== initialFlow.current?.id) throw new Error("填写步骤已改变，请刷新后重新上传。");
      uploadFields = { ...uploadFields, flow_epoch: initialFlow.flow_epoch, expected_revision: initialFlow.revision, node_id: initialFlow.current.id };
    }
    uploadFields = receiptCardBody(context, uploadFields);
  }
  const encoded = definition.base64;
  if (typeof encoded !== "string" || encoded.length > Math.ceil(20 * 1024 * 1024 / 3) * 4 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))
    throw new Error("文件必须是有效 Base64，且不超过 20 MiB。");
  let raw;
  try { raw = atob(encoded); } catch { throw new Error("文件 Base64 不正确。"); }
  const bytes = Uint8Array.from(raw, (character) => character.charCodeAt(0));
  const file = new File([bytes], definition.filename || "attachment", { type: definition.content_type || "application/octet-stream" });
  if (!active()) throw new Error("文件操作上下文已失效。");
  const result = await uploadMultipart(jobScope ? "/manage/files/upload" : "/files/upload", uploadFields, file, { ...options, isCurrent: active });
  if (!active()) throw new Error("页面或流程步骤已切换，文件已上传但尚未提交任务。");
  if (targetJob?.task_flow || targetJob?.revision?.current > 0) {
    const refreshed = await selectedJobForFile(definition, options);
    if (!active() || !sameManagementStep(targetJob, refreshed)) throw new Error("处理步骤已改变，附件尚未提交。");
  }
  return result;
}
async function readFile(definition, options = {}) {
  options = managementOptions(options);
  const loadId = queueLoadId;
  await selectedJobForFile(definition, options);
  const metadata = await api("/manage/files?" + new URLSearchParams({ job_id: definition.job_id }), undefined, "GET", options);
  const file = metadata.find((item) => item.id === definition.file_id && item.job_id === definition.job_id);
  if (!file) throw new Error("文件不属于此任务。");
  const limit = Math.min(1048576, definition.max_bytes || 1048576);
  if (file.size > limit) throw new Error("文件超过 AI 读取上限，请使用下载链接。");
  const response = await fileResponse("/manage/files/" + encodeURIComponent(file.id) + "/download", options);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length > limit) throw new Error("文件超过 AI 读取上限，请使用下载链接。");
  if (options.signal?.aborted || loadId !== queueLoadId || queueProductId !== definition.product_id || tab !== "jobs") throw new Error("文件操作上下文已失效。");
  let raw = "";
  for (let offset = 0; offset < bytes.length; offset += 16384)
    raw += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
  return { file_id: file.id, filename: file.filename, content_type: file.content_type, size: bytes.length, base64: btoa(raw) };
}
function clearDeliveryBlobs() {
  for (const entry of deliveryBlobs.values()) if (entry.url) URL.revokeObjectURL(entry.url);
  deliveryBlobs.clear();
}
async function deliveryBlob(file, context) {
  for (const [key, entry] of deliveryBlobs) {
    if (!entry.context.active()) {
      if (entry.url) URL.revokeObjectURL(entry.url);
      deliveryBlobs.delete(key);
    }
  }
  let entry = deliveryBlobs.get(file.id);
  if (!entry) {
    entry = { context, url: null };
    deliveryBlobs.set(file.id, entry);
    entry.promise = (async () => {
      const response = await fileResponse("/files/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(receiptCardBody(context, { file_id: file.id })) });
      const blob = await response.blob();
      if (!context.active() || deliveryBlobs.get(file.id) !== entry) return null;
      const canonicalType = ["image/png", "image/jpeg", "image/webp"].includes(file.content_type) ? file.content_type : "application/octet-stream";
      entry.url = URL.createObjectURL(new Blob([blob], { type: canonicalType }));
      return entry.url;
    })().catch((error) => { if (deliveryBlobs.get(file.id) === entry) deliveryBlobs.delete(file.id); throw error; });
  }
  return entry.promise;
}
async function downloadDeliveryFile(file) {
  const context = receiptRequestContext();
  const url = await deliveryBlob(file, context);
  if (!url || !context.active()) return;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  anchor.click();
}
async function previewDeliveryImage(file) {
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.content_type)) throw new Error(tr("图片格式无效，请下载检查。", "Invalid image format. Download to inspect."));
  const context = receiptRequestContext();
  const url = await deliveryBlob(file, context);
  if (!url || !context.active()) return;
  const target = document.querySelector(`[data-delivery-preview="${file.id}"]`);
  if (target) target.innerHTML = `<img class="product-cover" src="${esc(url)}" alt="${esc(file.filename)}">`;
}
function bindManagementDownloads(files, isCurrent, options) {
  document.querySelectorAll("[data-management-download]").forEach((button) => {
    const file = files.find((entry) => entry.id === button.dataset.managementDownload);
    if (!file || button.extoreDownloadBound) return;
    button.extoreDownloadBound = true;
    button.addEventListener("click", () => perform(async () => {
    if (!isCurrent()) return;
    const response = await fileResponse("/manage/files/" + encodeURIComponent(file.id) + "/download", options);
    const blob = await response.blob();
    if (!isCurrent()) return;
    const url = URL.createObjectURL(blob), anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    }, button));
  });
}

function on(id, handler, event = "click") {
  const node = $(id);
  if (node) node.addEventListener(event, (e) => { if (event === "submit") e.preventDefault(); return perform(handler, node); });
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
  window.ExtoreTaskFlow?.dispose(app);
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
window.addEventListener("hashchange", () => {
  if (location.pathname === "/cli/owner") start();
});
window.addEventListener("pagehide", () => { window.ExtoreProducts?.dispose?.($("#workspace")); window.ExtoreTaskFlow?.dispose(app); ownerCliApproval?.dispose(); deviceCliApproval?.dispose(); pipelineAuthView?.dispose(); progressBoardController?.dispose(); });

async function home() {
  window.ExtoreTaskFlow?.dispose(app);
  clearDeliveryBlobs();
  queueLoadId++;
  const generation = queueLoadId;
  const pathname = location.pathname;
  const hash = location.hash;
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = "";
  receiptRevisionRequest = null;
  currentToken = "";
  currentBatch = null;
  batchSelection = "";
  batchRetryOnly = false;
  currentProduct = null;
  currentJob = null;
  currentVariant = null;
  currentCardAttributes = {};
  currentEntitlements = null;
  const list = await api("/products");
  if (generation !== queueLoadId || location.pathname !== pathname || location.hash !== hash) return;
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
  try {
    const incoming = window.ExtoreProxyRouting?.consumeIncoming();
    if (incoming) await exchangeCode(incoming);
  } catch (error) { $("#error").textContent = error.message; }
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
  currentProduct = data.product || data.items.find((item) => item.accepted !== false && item.product)?.product || null;
  currentJob = null;
  if (batchSelection) {
    const item = data.items.find((entry) => entry.card_id === batchSelection);
    if (item?.job && !batchRetryOnly) {
      currentProduct = batchProduct(item);
      currentJob = item.job;
      currentVariant = item.variant || item.job.variant || null;
      acceptReceiptCardInfo(item);
      renderReceipt(item.job);
      return;
    }
    if (item && (batchRetryOnly || !item.job)) {
      currentProduct = batchProduct(item);
      currentJob = item.job || null;
      currentVariant = item.variant || item.job?.variant || null;
      acceptReceiptCardInfo(item);
      redemptionForm();
      return;
    }
    selectBatchCard();
  }
  selectBatchCard();
  if (batchPending().length) redemptionForm();
  else renderBatch(data);
}
async function exchangePastedCode(options = {}) {
  if (location.pathname !== "/" || options.signal?.aborted) throw new Error("请先打开卡密兑换页。");
  const value = document.getElementById("code")?.value;
  if (typeof value !== "string" || !value.trim()) throw new Error("请先把卡密粘贴到页面输入框。");
  return exchangeCode(value, options);
}
async function exchangeCode(code, options = {}) {
  const context = receiptRequestContext(options);
  const generation = receiptGeneration;
  const routingPage = queueLoadId;
  if (!options.skipProxyRouting) {
    const routed = String(code).trim().split(/[\s,，;；]+/).some((part) => window.ExtoreProxyRouting ? window.ExtoreProxyRouting.isRoutedCode(part) : /^EXR[0-9]+/i.test(part));
    if (routed && !window.ExtoreProxyRouting) throw new Error(tr("浏览器路由未能加载，请刷新后重试。", "Routing could not load. Refresh and try again."));
    if (routed) {
      const grouped = await window.ExtoreProxyRouting.routeCodes(code);
      if (!context.active() || routingPage !== queueLoadId) return window.ExtoreProxyRouting.publicResult(grouped);
      if (grouped.groups.length) {
        window.ExtoreMotion?.disposeHome?.(app);
        return window.ExtoreProxyRouting.renderRoutingChoice(app, grouped, {
          tr, esc,
          onLocal: (local) => context.active() && routingPage === queueLoadId ? exchangeCode(local, { ...options, skipProxyRouting: true }) : undefined,
          onBack: () => { history.replaceState({}, "", "/"); start(); },
        });
      }
      code = grouped.localCodes.join("\n");
    }
  }
  if (!context.active()) return;
  const multiple = splitCodes(code).length > 1 || /[\n,，;；]/.test(String(code).trim());
  const data = await api(multiple ? "/batch/exchange" : "/exchange", { code }, "POST", options);
  if (!context.active()) return data;
  const render = () => {
    if (!context.active()) return;
    currentToken = data.token || "";
    currentProduct = data.product;
    currentJob = data.job || null;
    currentVariant = data.variant || data.job?.variant || data.items?.[0]?.variant || null;
    acceptReceiptCardInfo(data);
    currentBatch = data.batch ? data : null;
    batchSelection = "";
    batchRetryOnly = false;
    if (currentToken) history.pushState({}, "", "/receipt#" + currentToken);
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
  const result = await api(currentBatch?.partial && Array.isArray(params) ? "/batch/redeem" : "/redeem", body, "POST", options);
  if (context.active()) {
    selectBatchCard();
    if (result.batch) {
      const rendered = options.batchUploadErrors?.length ? {
        ...result,
        results: [...(result.results || []), ...options.batchUploadErrors.map((error) => ({
          card_id: error.card_id, status: "error", error: error.error,
        }))],
      } : result;
      currentBatch = rendered;
      openBatch(rendered);
    } else renderReceipt(result);
  }
  return result;
}
async function retryOriginalReceipt(options = {}) {
  receiptGeneration++;
  const context = receiptRequestContext(options);
  stopPoll();
  const result = await api("/retry", receiptCardBody(context), "POST", options);
  if (context.active()) {
    if (currentBatch && context.cardId) {
      const item = currentBatch.items.find((entry) => entry.card_id === context.cardId);
      if (item) item.job = result;
    }
    renderReceipt(result);
  }
  return result;
}
async function restartTaskFlowReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const flow = window.ExtoreTaskFlow?.flowOf(currentJob, currentProduct) || currentJob?.task_flow;
  if (!flow) throw new Error(tr("当前任务不使用编排", "This task does not use a task flow"));
  const result = await api("/task-flow/restart", receiptCardBody(context, {
    flow_epoch: flow.flow_epoch, expected_revision: flow.revision,
  }), "POST", options);
  if (context.active()) {
    const nextJob = result.job || result;
    if (currentBatch && context.cardId) {
      const item = currentBatch.items.find((entry) => entry.card_id === context.cardId);
      if (item) item.job = nextJob;
    }
    renderReceipt(nextJob);
  }
  return result;
}
async function readReceipt(options = {}) {
  const context = receiptRequestContext(options);
  let result;
  if (currentBatch || currentProduct) {
    result = await api(currentBatch?.partial ? "/batch/receipt" : "/receipt", { token: context.token }, "POST", options);
  } else {
    try {
      result = await api("/batch/receipt", { token: context.token }, "POST", options);
    } catch (error) {
      // A legacy single-card grant is not a batch. No other failure should be
      // hidden by a second request or treated as permission to use a fallback.
      if (error.status !== 404 || !context.active()) throw error;
      result = await api("/receipt", { token: context.token }, "POST", options);
    }
  }
  if (context.active()) {
    currentProduct = result.product;
    currentJob = result.job || null;
    currentVariant = result.variant || result.job?.variant || result.items?.[0]?.variant || null;
    acceptReceiptCardInfo(result);
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
function deliveryTextControls(generation, index, label) {
  const id = `delivery-text-${generation}-${index}`;
  return `<div class="delivery-text-actions"><button id="${id}-copy" class="secondary" type="button" aria-label="${esc(tr("复制原文：", "Copy original text: ") + label)}">${tr("复制文本", "Copy text")}</button><button id="${id}-download" class="secondary" type="button" aria-label="${esc(tr("下载原文：", "Download original text: ") + label)}">${tr("下载文本", "Download text")}</button><span id="${id}-status" class="caption" role="status" aria-live="polite"></span></div><div id="${id}-manual" class="delivery-text-manual" hidden><label for="${id}-raw">${tr("原文，可手动复制", "Original text, available to copy manually")}</label><textarea id="${id}-raw" readonly rows="3" spellcheck="false"></textarea></div>`;
}
function deliveryTextDownload(key, text) {
  const safeKey = String(key).toLowerCase().replace(/[^a-z0-9_-]/g, "-").replace(/-+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "content";
  let extension = "txt";
  if (/(?:^|_)json$/.test(String(key)) || key === "summary" || /^\s*[\[{]/.test(text)) {
    try { JSON.parse(text); extension = "json"; } catch { /* Ordinary text remains a text download. */ }
  }
  return { filename: `extore-${safeKey}.${extension}`, type: extension === "json" ? "application/json;charset=utf-8" : "text/plain;charset=utf-8" };
}
function bindDeliveryText(content, entries, context, generation, jobScope) {
  for (const { index, key, text } of entries) {
    const id = `delivery-text-${generation}-${index}`;
    const copy = $("#" + id + "-copy"), download = $("#" + id + "-download"), status = $("#" + id + "-status");
    const active = (button) => context.active() && generation === deliveryRevealGeneration && currentJob?.id === jobScope.id && currentJob?.revision?.current === jobScope.revision && currentJob?.state !== "destroyed" && $("#content") === content && button?.isConnected && content.contains(button);
    copy?.addEventListener("click", async () => {
      if (!active(copy) || copy.disabled) return;
      copy.disabled = true;
      const copied = await writeClipboard(text);
      if (!active(copy)) return;
      copy.disabled = false;
      status.textContent = copied ? tr("文本已复制", "Text copied") : tr("复制失败，请手动复制下方原文。", "Could not copy. Select the original text below.");
      if (!copied) {
        const manual = $("#" + id + "-manual"), field = $("#" + id + "-raw");
        if (manual && field) { manual.hidden = false; field.value = text; field.focus(); field.select(); }
      }
    });
    download?.addEventListener("click", () => {
      if (!active(download)) return;
      const definition = deliveryTextDownload(key, text);
      let url;
      try { url = URL.createObjectURL(new Blob([text], { type: definition.type })); }
      catch { status.textContent = tr("下载失败，请复制文本保存。", "Could not download. Copy the text to save it."); return; }
      const blobKey = "text:" + url;
      const entry = { context: { active: () => active(download) }, url };
      deliveryBlobs.set(blobKey, entry);
      const revoke = () => {
        if (deliveryBlobs.get(blobKey) !== entry) return;
        URL.revokeObjectURL(url);
        deliveryBlobs.delete(blobKey);
      };
      try {
        if (!active(download)) { revoke(); return; }
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = definition.filename;
        anchor.click();
        status.textContent = tr("已开始下载", "Download started");
      } catch {
        revoke();
        if (active(download)) status.textContent = tr("下载失败，请复制文本保存。", "Could not download. Copy the text to save it.");
      } finally {
        setTimeout(revoke, 30000);
      }
    });
  }
}
async function revealReceipt(options = {}) {
  const context = receiptRequestContext(options);
  const revealGeneration = ++deliveryRevealGeneration;
  const jobScope = { id: currentJob?.id, revision: currentJob?.revision?.current };
  const viewPolicy = currentProduct?.view_policy;
  const fields = currentProduct?.outputs || [];
  const result = await api(
    "/receipt/reveal",
    receiptCardBody(context, options.revision === undefined ? {} : { revision: options.revision }),
    "POST",
    options,
  );
  if (context.active() && revealGeneration === deliveryRevealGeneration) {
    const content = $("#content");
    if (content) {
      const output = result.output || { content: result.content };
      const textEntries = [];
      content.innerHTML = Object.entries(output)
        .map(([key, value], index) => {
          const definition = fields.find((field) => field.key === key);
          const attachmentField = isAttachmentField(definition || {}) ? definition : { type: "file" };
          const ids = fieldAttachmentIds(attachmentField, value);
          const files = ids.map((id) => (result.files || []).find((item) => item.id === id && (!item.field_key || item.field_key === key))).filter(Boolean);
          const images = ["image", "images"].includes(definition?.type);
          const display = definition?.type === "boolean" && ["true", "false"].includes(value)
            ? value === "true" ? tr("是", "Yes") : tr("否", "No")
            : definition?.type === "select" ? localized((definition.options || []).find((option) => option.value === value)?.label) || value : value;
          const text = typeof value === "string" ? value : ["number", "boolean"].includes(typeof value) ? String(value) : null;
          const label = localized(definition?.label) || key;
          const textControls = !files.length && !isAttachmentField(definition || {}) && text !== null
            ? (textEntries.push({ index, key, text }), deliveryTextControls(revealGeneration, index, label)) : "";
          const contents = files.length ? files.map((file) => `<div>${images ? `<button class="secondary" data-delivery-image="${esc(file.id)}">${tr("查看图片", "View image")} · ${esc(file.filename)}</button><div data-delivery-preview="${esc(file.id)}"></div>` : ""}<button class="secondary" data-delivery-file="${esc(file.id)}">${tr("下载文件", "Download")} · ${esc(file.filename)}</button><p class="caption">${Math.ceil(file.size / 1024)} KiB</p></div>`).join("") : `<pre class="result">${esc(display)}</pre>${textControls}`;
          return `<div class="delivery-field"><h3>${esc(label)}</h3>${contents}</div>`;
        })
        .join("");
      bindDeliveryText(content, textEntries, context, revealGeneration, jobScope);
    }
    for (const file of result.files || []) {
      on('[data-delivery-file="' + file.id + '"]', () => downloadDeliveryFile(file));
      on('[data-delivery-image="' + file.id + '"]', () => previewDeliveryImage(file));
    }
    if (viewPolicy === "once") $("#reveal")?.remove();
  }
  return result;
}
async function requestReceiptRevision(message, options = {}) {
  const context = receiptRequestContext(options);
  const job = currentJob;
  const text = String(message || "").trim();
  const revision = job?.revision?.current ?? 0;
  if (!context.token || !job?.entitlements?.can_request || job.state !== "succeeded")
    throw new Error(tr("当前交付不能申请修改，请刷新领取状态。", "This delivery cannot request a revision. Refresh its status."));
  if (!text || text.length > 10000)
    throw new Error(tr("请填写修改建议，最多 10000 个字符。", "Enter revision feedback, up to 10,000 characters."));
  if (options.expected_revision !== undefined && options.expected_revision !== revision)
    throw new Error(tr("交付轮次已改变，请刷新后再提交。", "The delivery revision changed. Refresh before submitting."));
  const key = JSON.stringify([context.token, context.cardId, revision]);
  if (receiptRevisionRequest?.key !== key) receiptRevisionRequest = null;
  if (receiptRevisionRequest?.promise) {
    if (receiptRevisionRequest.message !== text)
      throw new Error(tr("修改建议正在提交，请稍候。", "Revision feedback is being submitted. Please wait."));
    return receiptRevisionRequest.promise;
  }
  if (!receiptRevisionRequest || receiptRevisionRequest.message !== text) {
    if (!globalThis.crypto?.randomUUID)
      throw new Error(tr("浏览器无法生成安全的请求标识，请更新浏览器。", "This browser cannot generate a secure request ID. Update your browser."));
    receiptRevisionRequest = { key, message: text, id: globalThis.crypto.randomUUID(), promise: null };
  }
  const request = receiptRevisionRequest;
  request.promise = (async () => {
    const result = await api("/receipt/revisions", receiptCardBody(context, {
      request_id: request.id, expected_revision: revision, message: text,
    }), "POST", options);
    if (context.active()) {
      if (receiptRevisionRequest === request) receiptRevisionRequest = null;
      const nextJob = result.job || result;
      if (nextJob.id === job.id && typeof nextJob.state === "string") {
        if (currentBatch && context.cardId) {
          const item = currentBatch.items.find((entry) => entry.card_id === context.cardId);
          if (item) item.job = nextJob;
        }
        renderReceipt(nextJob);
      }
      try { await readReceipt(options); }
      catch {
        if (context.active()) toast(tr("修改申请已提交，状态刷新失败，请刷新页面。", "Revision requested. Refresh the page to reload its status."));
      }
    }
    return result;
  })();
  try { return await request.promise; }
  finally { request.promise = null; }
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
    deliveryRevealGeneration++;
    receiptRevisionRequest = null;
    clearDeliveryBlobs();
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
async function flowAction(operation, values = {}, options = {}) {
  if (!["start", "answer", "continue", "restart", "cancel"].includes(operation)) throw new Error("流程操作无效。");
  const context = receiptRequestContext(options), flow = customerFlow();
  if (!flow || !(flow.actions || []).includes(operation)) throw new Error("当前步骤不支持这个操作，请刷新后继续。");
  if (options.flow_epoch !== undefined && options.flow_epoch !== flow.flow_epoch || options.expected_revision !== undefined && options.expected_revision !== flow.revision || options.card_id !== undefined && options.card_id !== context.cardId) throw new Error("流程步骤已改变，请刷新后继续。");
  const step = customerStepKey(flow);
  const active = () => context.active() && step === customerStepKey(customerFlow());
  if (!active()) throw new Error("流程上下文已失效。");
  const body = receiptCardBody(context, { flow_epoch: flow.flow_epoch, expected_revision: flow.revision, ...(operation === "answer" ? { values } : {}) });
  const result = await api("/task-flow/" + operation, body, "POST", options);
  if (active()) {
    const next = result.job || result;
    if (currentBatch && context.cardId) {
      const member = currentBatch.items.find((item) => item.card_id === context.cardId);
      if (member) member.job = next;
    }
    renderReceipt(next);
  }
  return result;
}
function renderTaskFlow(job = null) {
  const flowModule = window.ExtoreTaskFlow;
  if (!flowModule) return false;
  const flow = flowModule.flowOf(job, currentProduct);
  if (!flow || flow.phase === "ended") return false;
  const context = receiptRequestContext();
  const handled = flowModule.render({
    app, product: currentProduct, job, flow, lang,
    token: context.token, cardId: context.cardId,
    variant: job?.variant || currentVariant,
    receiptUrl: location.origin + "/receipt#" + context.token,
    active: context.active, api, upload: uploadMultipart, uploadLimit: uploadFileLimit, confirm: (message) => confirm(message),
    copy: writeClipboard, notify: toast, refresh: readReceipt, renderSteps: fulfillmentStepsMarkup,
    animationPaused: waitingAnimationPaused,
    setAnimationPaused: (value) => { waitingAnimationPaused = value; },
    back: () => { selectBatchCard(); openBatch(currentBatch); },
    onResult: async (result) => {
      if (!context.active()) return;
      const nextJob = result.job || result;
      if (!nextJob.id || !nextJob.state) { await readReceipt(); return; }
      if (currentBatch && context.cardId) {
        const item = currentBatch.items.find((entry) => entry.card_id === context.cardId);
        if (item) item.job = nextJob;
      }
      renderReceipt(nextJob);
    },
  });
  if (handled) {
    receiptMotion?.dispose();
    receiptMotion = null;
    receiptViewKey = "";
    if (job) currentJob = job;
    pollTaskFlow();
  }
  return handled;
}
function pollTaskFlow() {
  const context = receiptRequestContext();
  timer = setTimeout(async () => {
    if (!context.active()) return;
    try { await readReceipt(); }
    catch (error) {
      if (context.active()) { toast(error.message); pollTaskFlow(); }
    }
  }, document.hidden ? 15000 : 2500);
}
function redemptionForm() {
  stopPoll();
  if ((!currentBatch || batchSelection) && renderTaskFlow(currentJob)) return;
  window.ExtoreTaskFlow?.dispose(app);
  receiptMotion?.dispose();
  receiptMotion = null;
  receiptViewKey = "";
  window.ExtoreWebMCP?.refresh();
  if (currentBatch?.partial && !batchSelection && window.ExtoreBatchRedemption) {
    window.ExtoreBatchRedemption.renderForm(currentBatch, batchRedemptionOptions());
    return;
  }
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
        const copy = index === 0 && pending.length > 1 && itemProduct.parameters.some((field) => !isAttachmentField(field))
          ? `<button type="button" class="secondary" id="copy-params">${tr("填到其余卡密", "Copy to the other codes")}</button>`
          : "";
        const saved = item.job?.state === "needs_input" ? item.job.params || {} : {};
        return `<section class="batch-card"><h3>${tr("卡密", "Code")} ···${esc(item.suffix || "")}${variant}</h3>${item.job?.state === "needs_input" ? `<p class="error">${esc(item.job.message)}</p>` : ""}${frozenCardInfo(item.card_attributes || item.job?.card_attributes, item.entitlements || item.job?.entitlements)}${copy}${itemProduct.parameters.map((field) => parameterFields(`param-${item.card_id}-`, field, saved[field.key] || "")).join("")}</section>`;
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
  app.innerHTML = `<div class="narrow">${back}<h1>${esc(p.name)}</h1><div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("填写信息", "Your details")}</div><div class="step">${tr("领取商品", "Collect")}</div></div>${p.image ? `<img class="product-cover" src="${esc(p.image)}" alt="">` : ""}<div class="markdown">${md(p.description)}</div>${pending ? "" : frozenCardInfo(currentCardAttributes, currentEntitlements)}<div class="panel"><h2>${tr("确认兑换信息", "Confirm your details")}</h2><form id="form">${fields}<button type="submit" class="full">${tr("确认兑换", "Confirm redemption")}</button>${countNote}<div id="error" class="error" role="alert"></div></form>${started.length ? `<div class="batch-started"><h3>${tr("已开始处理", "Already started")}</h3><div class="batch-list">${started.map(batchItemButton).join("")}</div></div>` : ""}</div></div>`;
  on("#batch-back", () => {
    selectBatchCard();
    openBatch(currentBatch);
  });
  on("#copy-params", () => {
    const source = pending[0];
    for (const field of batchProduct(source).parameters) {
      if (isAttachmentField(field)) continue;
      const from = document.getElementById(`param-${source.card_id}-${field.key}`);
      if (!from) continue;
      for (const item of pending.slice(1)) {
        if (!batchProduct(item).parameters.some((target) => target.key === field.key && target.type === field.type && (field.type !== "select" || (target.options || []).some((option) => option.value === from.value)))) continue;
        const target = document.getElementById(`param-${item.card_id}-${field.key}`);
        if (target) target.value = from.value;
      }
    }
    toast(tr("已填到其余卡密。文件仍需分别上传。", "Copied to the other codes. Files still need their own upload."));
  });
  bindBatchItems(started);
  if ((pending ? pending.some((item) => batchProduct(item).parameters.some(isAttachmentField)) : p.parameters.some(isAttachmentField))) void uploadFileLimit();
  form(async () => {
    const context = receiptRequestContext();
    if (!pending) {
      const params = {};
      for (const field of p.parameters) {
        const input = document.getElementById("param-" + field.key);
        if (isAttachmentField(field)) {
          params[field.key] = await uploadFieldFiles(input, field, document.getElementById("param-" + field.key + "-retained")?.value || "", "/files/upload", { token: context.token }, { isCurrent: context.active });
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
        if (isAttachmentField(field)) {
          params[field.key] = await uploadFieldFiles(input, field, document.getElementById(`param-${item.card_id}-${field.key}-retained`)?.value || "", "/files/upload", { token: context.token, card_id: item.card_id }, { isCurrent: context.active });
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
  if (data.partial && window.ExtoreBatchRedemption) {
    receiptMotion?.dispose();
    receiptMotion = null;
    receiptViewKey = "";
    window.ExtoreBatchRedemption.renderOverview(data, batchRedemptionOptions());
    pollBatch(data);
    return;
  }
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
function hasReceiptDelivery(job) {
  return job.state !== "destroyed" && job.delivery === "content" &&
    (job.state === "succeeded" || Boolean(job.last_delivery));
}
function revisionUnavailableReason(reason, remaining) {
  const reasons = {
    revoked: ["卡密已撤销，不能申请修改。", "This code was revoked. Revisions are unavailable."],
    rejected: ["任务已拒绝处理，不能申请修改。", "This task was rejected. Revisions are unavailable."],
    destroyed: ["交付内容已销毁。", "The delivery was destroyed."],
    expired: ["卡密已过期，不能申请修改。", "This code expired. Revisions are unavailable."],
    shop_disabled: ["店铺暂停服务，暂时不能申请修改。", "The shop is paused. Revisions are unavailable."],
    exhausted: ["修改额度已用完，仍可查看已交付内容。", "No revisions remain. Existing deliveries remain available."],
    in_progress: ["修改正在处理中，之前的交付仍可查看。", "A revision is in progress. Previous deliveries remain available."],
    not_delivered: ["本轮完成后可提交修改建议。", "You can request a revision after this round finishes."],
  };
  const value = Object.hasOwn(reasons, reason) ? reasons[reason] : reasons[remaining === 0 ? "exhausted" : "not_delivered"];
  return tr(...value);
}
function receiptRevisionMarkup(job) {
  const allowance = job.entitlements;
  if (!allowance || job.state === "destroyed") return "";
  const label = localized(allowance.label) || tr("修改额度", "Revision allowance");
  const round = Number.isSafeInteger(job.revision?.current) ? job.revision.current : 0;
  return `<section class="receipt-revisions"><div class="section-head"><h3>${esc(label)}</h3><span class="caption">${esc(tr(`剩余 ${allowance.remaining} / ${allowance.total}`, `${allowance.remaining} of ${allowance.total} remaining`))}</span></div>${round ? `<p class="caption">${tr(`第 ${round} 轮修改`, `Revision ${round}`)}</p>` : ""}${job.revision?.is_revision && job.revision.message ? `<details class="revision-feedback"><summary>${tr("本轮修改建议", "Current revision feedback")}</summary><p>${esc(job.revision.message)}</p></details>` : ""}${allowance.can_request ? `<form id="receipt-revision-form"><div class="field"><label for="revision-message">${tr("需要修改什么？", "What would you like changed?")}</label><textarea id="revision-message" required maxlength="10000" rows="4" placeholder="${tr("说明需要调整的内容，保留原有需求与本次修改的区别。", "Describe what should change from the delivered result.")}"></textarea></div><p class="caption">${tr("提交后扣除 1 次修改额度，并重新进入处理队列。等待期间仍可查看之前的交付。", "Submitting uses one revision and returns the task to the queue. Previous deliveries remain available while you wait.")}</p><button id="request-revision" type="submit">${tr("提交修改建议", "Request a revision")}</button></form>` : `<p class="caption">${esc(revisionUnavailableReason(allowance.reason, allowance.remaining))}</p>`}</section>`;
}
function receiptDeliveryVersions(job) {
  const versions = (job.deliveries || []).filter((value) => Number.isSafeInteger(value.revision) && value.revision >= 0);
  if (!hasReceiptDelivery(job) || versions.length < 2) return "";
  const latest = job.last_delivery?.revision ?? versions[versions.length - 1].revision;
  return `<div class="field delivery-version-field"><label for="delivery-revision">${tr("交付版本", "Delivery version")}</label><select id="delivery-revision">${versions.map((version) => `<option value="${version.revision}" ${version.revision === latest ? "selected" : ""}>${version.revision === 0 ? tr("初次交付", "Original delivery") : tr(`第 ${version.revision} 轮修改`, `Revision ${version.revision}`)}${version.revision === latest ? tr(" · 最新", " · Latest") : ""}</option>`).join("")}</select></div>`;
}
function renderReceipt(j) {
  currentJob = j;
  currentCardAttributes = j.card_attributes || currentCardAttributes;
  currentEntitlements = j.entitlements || null;
  stopPoll();
  if (renderTaskFlow(j)) return;
  window.ExtoreTaskFlow?.dispose(app);
  window.ExtoreWebMCP?.refresh();
  const p = currentProduct;
  const waiting = ["processing", "queued"].includes(j.state);
  const viewKey = JSON.stringify([currentToken, j.id, j.state, j.can_retry, j.retry_mode, j.delivery, j.view_policy, p.name, j.support_email || p.support_email, lang, j.variant || currentVariant, j.entitlements, j.revision, j.deliveries, j.last_delivery]);
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
  app.innerHTML = `<div class="narrow ${waiting ? "receipt-waiting" : ""}">${batchBack}<h1>${esc(p.name)}</h1>${suffixLine}${variant?.name ? `<p class="caption">${tr("规格：", "Variant: ")}${esc(variant.name)}</p>` : ""}${frozenCardInfo(currentCardAttributes, currentEntitlements, false)}<div class="steps"><div class="step active">${tr("卡密已验证", "Code verified")}</div><div class="step active">${tr("信息已提交", "Details submitted")}</div><div class="step ${j.state === "succeeded" ? "active" : ""}">${tr("领取商品", "Collect")}</div></div><section class="panel receipt-panel">${waiting ? '<div class="paper-divider waiting-top" aria-hidden="true"></div>' : ""}<div class="section-head"><h2>${tr("兑换进度", "Redemption progress")}</h2>${status(j)}</div><p id="customer-message"></p><p id="queue-position" class="caption"></p><div id="fulfillment-steps"></div>${waiting ? `<div class="progress" role="progressbar" aria-label="${tr("处理进度", "Processing progress")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${j.progress}"><span></span></div><p id="progress-label" class="caption"></p>${window.ExtoreMotion?.waitingMarkup(lang) || ""}<button id="motion-toggle" class="secondary motion-toggle" aria-pressed="${waitingAnimationPaused}">${waitingAnimationPaused ? tr("播放动画", "Play animation") : tr("暂停动画", "Pause animation")}</button>${reminderMarkup(j)}` : ""}${receiptDeliveryVersions(j)}<div id="content"></div><div class="actions">${hasReceiptDelivery(j) ? `<button id="reveal">${tr(j.view_policy === "once" ? "领取内容（仅一次）" : "查看交付内容", j.view_policy === "once" ? "Reveal once" : "View your goods")}</button>` : ""}${j.can_retry ? `<button id="retry">${tr(j.state === "needs_input" && j.retry_mode === "reuse" ? "使用原资料重试" : "检查信息并重试", j.state === "needs_input" && j.retry_mode === "reuse" ? "Retry with original details" : "Review details and retry")}</button>` : ""}${hasReceiptDelivery(j) || j.state === "succeeded" ? `<button id="destroy" class="danger">${tr("立即销毁", "Destroy now")}</button>` : ""}</div>${receiptRevisionMarkup(j)}<div id="error" class="error" role="alert"></div>${j.state === "destroyed" ? `<p class="caption">${tr("内容已永久删除，此链接无法再领取。", "The content has been deleted. This link can no longer reveal it.")}</p>` : ""}${waiting ? '<div class="paper-divider waiting-bottom" aria-hidden="true"></div>' : ""}</section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(location.origin + "/receipt#" + currentToken)}</p><button id="copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${batchItem ? tr("这个链接包含全部卡密，有效期 30 天，请勿转发给他人。", "This link covers every code. Valid for 30 days. Do not forward it.") : tr("有效期 30 天。链接是领取凭证，请勿转发给他人。", "Valid for 30 days. Anyone with this link can access the receipt.")}</p></div></div>`;
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
    const revision = $("#delivery-revision")?.value;
    await revealReceipt(revision === undefined || revision === "" ? {} : { revision: Number(revision) });
  });
  on("#destroy", async () => {
    if (!confirm(tr("永久删除所有版本的交付内容并关闭领取。继续销毁？", "Permanently delete every delivery version and disable access?"))) return;
    await destroyReceipt();
  });
  on("#delivery-revision", () => {
    deliveryRevealGeneration++;
    clearDeliveryBlobs();
    const content = $("#content");
    if (content) content.innerHTML = "";
  }, "change");
  on("#receipt-revision-form", async () => {
    const button = $("#request-revision");
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try { await requestReceiptRevision($("#revision-message").value); }
    finally { if (button?.isConnected) button.disabled = false; }
  }, "submit");
  on("#batch-back", () => {
    selectBatchCard();
    openBatch(currentBatch);
  });
  on("#retry", async () => {
    if (j.task_flow) { await restartTaskFlowReceipt(); return; }
    if (j.state === "needs_input" && j.retry_mode === "reuse") { await retryOriginalReceipt(); return; }
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
async function passkey(register = false, name = null, options = {}) {
  options = managementOptions(options);
  const generation = routeLoadId, pathname = location.pathname;
  const active = () => (options.isCurrent ? options.isCurrent() : generation === routeLoadId && pathname === location.pathname);
  const publicOptions = await api(
    "/auth/" + (register ? "register" : "login") + "/options",
    {}, "POST", options,
  );
  if (!active()) throw new DOMException("Account page changed", "AbortError");
  publicOptions.challenge = decode(publicOptions.challenge);
  if (register) {
    publicOptions.user.id = decode(publicOptions.user.id);
    publicOptions.excludeCredentials?.forEach((x) => (x.id = decode(x.id)));
  } else publicOptions.allowCredentials?.forEach((x) => (x.id = decode(x.id)));
  const c = register
    ? await navigator.credentials.create({ publicKey: publicOptions, signal: options.signal })
    : await navigator.credentials.get({ publicKey: publicOptions, signal: options.signal });
  if (!active()) throw new DOMException("Account page changed", "AbortError");
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
    name: name || $("#key-name")?.value || "我的 Passkey",
  }, "POST", options);
  if (!active()) throw new DOMException("Account page changed", "AbortError");
  return api("/auth/status");
}
async function admin() {
  stopPoll();
  const generation = queueLoadId;
  const active = () => generation === queueLoadId && location.pathname === "/admin";
  const auth = await api("/auth/status");
  if (!active()) return;
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
    navigate("/account/login");
    return;
  }
  const available = await api("/admin/products");
  if (!active()) return;
  products = available;
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
  "product.delete": "删除与恢复商品（保留旧卡密和任务）",
  "product.purge": "彻底删除商品与清空回收站（不可恢复商品）",
  "queue.monitor": "只看进度看板（不含任务内容）",
};
function acceptAuth(auth) {
  if (JSON.stringify([authStatus.session_id, managementAuthority(), authStatus.permissions || []]) !== JSON.stringify([auth.session_id, managementAuthority(auth), auth.permissions || []])) {
    progressBoardController?.dispose(); progressBoardController = null;
    if (tab === "board" && $("#workspace")) $("#workspace").innerHTML = "";
    progressBoardShopId = ""; progressBoardProductId = ""; progressBoardView = "active";
  }
  const sameProductSession = role === auth.role && managedProductId === (auth.product_id || null) && authStatus.session_id && authStatus.session_id === auth.session_id;
  authStatus = auth;
  role = auth.role;
  permissions = auth.permissions || [];
  managedProductId = auth.product_id || null;
  managementName = auth.link_name || "";
  managementExpires = auth.link_expires || null;
  managementMaxUses = auth.max_uses || 1;
  managementMaxCLIUses = auth.max_cli_uses ?? 1;
  managementRemainingCLIUses = auth.remaining_cli_uses ?? Math.max(0, managementMaxCLIUses - (auth.cli_uses || 0));
  managementLinkId = auth.link_id || auth.staff_id || null;
  if (!sameProductSession) { managedProductDeleted = false; managedProductPurged = false; }
  if (location.pathname === "/admin" && role === "admin" && $("#management-identity")) $("#management-identity").textContent = managementIdentity();
}
function managementIdentity() {
  if (role === "staff") return managementName + " · 只管理被授权的商品。";
  if (window.ExtoreAccount?.rootScope(authStatus)) return "超级管理员 · 平台范围";
  const name = typeof authStatus.shop_name === "string" && authStatus.shop_name.trim() ? authStatus.shop_name : tr("店铺", "Shop");
  const email = typeof authStatus.shop_email === "string" ? authStatus.shop_email : "";
  return name + (email ? " · " + email : "");
}
const permitted = (permission) =>
  role === "admin" || permissions.includes(permission);
function managementTabs() {
  const items = [
    ["products", "商品", "product.edit"],
    ["jobs", "处理队列", "queue.view"],
    ["board", tr("进度看板", "Progress board"), "queue.monitor"],
    ["cards", "卡密", "cards.manage"],
    ["staff", "管理链接", "links.delegate"],
    ["events", "事件记录", "events.manage"],
  ].filter(([key, , permission]) => permitted(permission) || (key === "board" && permitted("queue.view")) || (key === "products" && (permitted("product.delete") || permitted("product.purge"))));
  items.push(["sessions", "会话与审计"]);
  if (role === "admin") {
    items.push(["security", "账户安全"], ["profiles", "商品处理器配置"], ["proxy", "兑换路由"]);
    if (window.ExtoreAccount?.rootScope(authStatus)) items.push(["shops", "店铺"], ["mail", "邮箱服务器"]);
  }
  return items;
}
function shell() {
  progressBoardController?.dispose(); progressBoardController = null;
  const items = managementTabs();
  if (!items.some(([key]) => key === tab)) tab = items[0]?.[0] || "";
  app.innerHTML = `<div class="admin-top"><div><h1>${role === "staff" ? "商品管理" : "商家后台"}</h1><p id="management-identity" class="muted">${esc(managementIdentity())}</p></div><button id="logout" class="secondary">退出登录</button></div><nav class="tabs" aria-label="管理导航">${items.map(([v, label]) => `<button data-tab="${v}" class="${tab === v ? "active" : ""}">${label}</button>`).join("")}</nav><div id="workspace"></div>`;
  on("#logout", async () => {
    progressBoardController?.dispose(); progressBoardController = null;
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
  window.ExtoreProducts?.dispose?.($("#workspace"));
  progressBoardController?.dispose(); progressBoardController = null;
  proxyConfigView?.dispose();
  proxyConfigView = null;
  pipelineAuthView?.dispose();
  pipelineAuthView = null;
  accountView?.dispose();
  accountView = null;
  queueLoadId++;
  window.ExtoreWebMCP?.refresh();
  if (tab === "products") {
    if (role === "staff") {
      if (permitted("product.edit")) products = [await api("/manage/product")];
      else products = await api("/manage/products?view=" + productsView);
      const product = products.find((item) => item.id === managedProductId);
      if (product) { managedProductPurged = product.purged === true || product.purged_at != null; managedProductDeleted = managedProductPurged || product.deleted === true || product.deleted_at != null; }
    }
    await renderProducts();
  }
  if (tab === "jobs") await renderJobs();
  if (tab === "board") renderProgressBoard();
  if (tab === "cards") await renderCards();
  if (tab === "staff") await renderStaff();
  if (tab === "events") await renderEvents();
  if (["security", "shops", "mail", "profiles"].includes(tab)) renderAccountTab();
  if (tab === "sessions") await renderSessions();
  if (tab === "proxy") renderProxyConfig();
}
function renderProgressBoard() {
  if (!permitted("queue.monitor") && !permitted("queue.view")) return;
  const generation = queueLoadId, pathname = location.pathname, authority = managementAuthority(), sessionId = authStatus.session_id;
  const granted = JSON.stringify(permissions), options = managementOptions();
  const active = () => generation === queueLoadId && tab === "board" && location.pathname === pathname && managementAuthority() === authority && authStatus.session_id === sessionId && JSON.stringify(permissions) === granted;
  if (!window.ExtoreProgressBoard) throw new Error(tr("进度看板未加载，请刷新页面。", "The progress board did not load. Refresh the page."));
  progressBoardController = window.ExtoreProgressBoard.mount({
    root: $("#workspace"), auth: { ...authStatus, permissions: [...permissions] },
    platform: authStatus.role === "admin" && authStatus.superadmin === true && authStatus.shop_id == null,
    shopId: progressBoardShopId, productId: progressBoardProductId, view: progressBoardView,
    language: () => lang, isCurrent: active,
    api: (path, body, method, extra = {}) => api(path, body, method, { ...options, ...extra }),
    onFilters: (filters) => { if (!active()) return; progressBoardShopId = filters.shopId; progressBoardProductId = filters.productId; progressBoardView = filters.view; window.ExtoreWebMCP?.refresh(); },
    copyPrompt: (promptOptions, host, current) => copyCLIPrompt({ origin: location.origin, ...promptOptions }, host, current),
  });
}
function renderProxyConfig() {
  const generation = queueLoadId, pathname = location.pathname;
  if (!window.ExtoreProxyConfig) throw new Error(tr("兑换路由未加载，请刷新页面。", "Redemption routes did not load. Refresh the page."));
  proxyConfigView = window.ExtoreProxyConfig.mount({ root: $("#workspace"), api, auth: authStatus,
    origin: location.origin, language: () => lang,
    isCurrent: () => generation === queueLoadId && tab === "proxy" && location.pathname === pathname });
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
  const buildPrompt = options.boardOnly ? window.ExtoreCliPrompts.buildBoard : options.processorWorkflow ? window.ExtoreCliPrompts.buildProcessorWorkflow : options.owner ? window.ExtoreCliPrompts.buildOwner : window.ExtoreCliPrompts.build;
  const prompt = buildPrompt({ language: lang, ...options, deviceCode: !options.owner });
  if (!isCurrent()) return;
  host.innerHTML = `<p id="cli-ai-feedback" class="caption" role="status">${tr("正在复制提示词…", "Copying prompt…")}</p><details id="cli-ai-preview" class="cli-prompt-preview"><summary>${tr("查看或手动复制提示词", "Preview or copy the prompt manually")}</summary><div class="field"><label for="cli-ai-prompt">${tr("给 AI 的 CLI 提示词", "CLI prompt for AI")}</label><textarea id="cli-ai-prompt" readonly spellcheck="false" rows="10">${esc(prompt)}</textarea></div><p class="caption">${options.owner ? tr("不含授权凭证。首次登录须由商家核对设备，再用 Passkey 明确批准全店管理权限。", "No credentials are included. The merchant must review the device and explicitly approve full shop access with a Passkey at first login.") : tr("不含授权凭证。AI 会显示设备码，请你在浏览器核对商品和权限后授权。", "No credentials are included. The AI displays a device code for you to review the product and permissions in your browser.")}</p></details>`;
  const input = $("#cli-ai-prompt");
  const copied = await writeClipboard(prompt);
  if (!isCurrent()) return;
  if (!copied) {
    $("#cli-ai-preview").open = true;
    input.focus();
    input.select();
  }
  $("#cli-ai-feedback").textContent = copied ? tr("提示词已复制，粘贴给你的 AI 即可。", "Prompt copied. Paste it to your AI.") : tr("无法访问剪贴板。提示词已展开并选中，请手动复制。", "Clipboard access is unavailable. The prompt is expanded and selected for manual copying.");
  toast(copied ? tr("AI 提示词已复制", "AI prompt copied") : tr("复制失败，已选中提示词，请手动复制。", "Could not copy. The prompt is selected; copy it manually."));
}
function productUIContext(visibleProducts = products) {
  const loadId = queueLoadId;
  const pathname = location.pathname;
  const scope = authStatus.shop_id, sessionId = authStatus.session_id;
  const requestOptions = managementOptions();
  const selectedProductsView = productsView;
  return {
    api: (path, body, method, options = {}) => api(path, body, method, { ...requestOptions, ...options }),
    workspace: $("#workspace"),
    products: visibleProducts,
    productView: productsView,
    role,
    sessionId: authStatus.session_id ?? null,
    canConfigure: permitted("fulfillment.configure"),
    canEdit: permitted("product.edit"),
    canDelete: permitted("product.delete"),
    canPurge: permitted("product.purge"),
    productId: role === "staff" ? managedProductId : null,
    shopId: authStatus.shop_id,
    superadmin: window.ExtoreAccount?.rootScope(authStatus) === true,
    getShopNames: () => api("/platform/shops", undefined, "GET", requestOptions),
    canManageCards: permitted("cards.manage"),
    lang,
    isCurrent: () =>
      loadId === queueLoadId && authStatus.shop_id === scope && authStatus.session_id === sessionId &&
      location.pathname === pathname &&
      tab === "products",
    onSaved: (updated) => {
      if (loadId === queueLoadId && authStatus.shop_id === scope && authStatus.session_id === sessionId && location.pathname === pathname && tab === "products") {
        if (selectedProductsView === "active") products = updated.filter((product) => product.deleted !== true && product.deleted_at == null && product.purged !== true && product.purged_at == null);
        else {
          const cached = new Map(products.map((product) => [product.id, product]));
          for (const product of updated) {
            if (product.deleted === true || product.deleted_at != null || product.purged === true || product.purged_at != null) cached.delete(product.id);
            else cached.set(product.id, product);
          }
          products = [...cached.values()];
        }
        const managed = updated.find((product) => product.id === managedProductId);
        if (managed) { managedProductPurged = managed.purged === true || managed.purged_at != null; managedProductDeleted = managedProductPurged || managed.deleted === true || managed.deleted_at != null; }
        const queued = updated.find((product) => product.id === queueProduct?.id);
        if (queued?.purged === true || queued?.purged_at != null) queueProduct = { ...queueProduct, deleted: true, purged: true, purged_at: queued.purged_at };
      }
    },
    onViewChange: (view, fallbackProducts = visibleProducts) => renderProducts(view, true, { view: selectedProductsView, products: fallbackProducts }),
    notify: toast,
    copyManagementLink,
    refreshTools: () => window.ExtoreWebMCP?.refresh(),
  };
}
async function renderProducts(requestedView = productsView, refresh = false, fallback = { view: productsView, products }) {
  const view = ["active", "deleted", "all"].includes(requestedView) ? requestedView : "active";
  productsView = view;
  let visibleProducts = products;
  if (refresh || view !== "active") {
    const loadId = ++queueLoadId;
    const pathname = location.pathname, authority = managementAuthority(), options = managementOptions();
    try {
      visibleProducts = await api((role === "staff" ? "/manage" : "/admin") + "/products?" + new URLSearchParams({ view }), undefined, "GET", options);
    } catch (error) {
      if (loadId !== queueLoadId || pathname !== location.pathname || tab !== "products" || authority !== managementAuthority() || authStatus.session_id !== options.expectedSessionId) return;
      productsView = fallback.view;
      await window.ExtoreProducts.render(productUIContext(fallback.products));
      if (loadId === queueLoadId && tab === "products" && $("#error")) $("#error").textContent = tr("商品列表加载失败，请重试：", "Could not load products. Try again: ") + error.message;
      return;
    }
    if (loadId !== queueLoadId || pathname !== location.pathname || tab !== "products" || authority !== managementAuthority() || authStatus.session_id !== options.expectedSessionId) return;
    if (view === "active") products = visibleProducts;
    const managed = visibleProducts.find((product) => product.id === managedProductId);
    if (managed) { managedProductPurged = managed.purged === true || managed.purged_at != null; managedProductDeleted = managedProductPurged || managed.deleted === true || managed.deleted_at != null; }
  }
  return window.ExtoreProducts.render(productUIContext(visibleProducts));
}
async function editProduct(p) {
  return window.ExtoreProducts.edit(productUIContext(), p);
}
function queueVisibleParams(job) {
  const hidden = new Set([...(job.protected_fields || []), ...(job.parameters || []).filter((field) => field.sensitive === true).map((field) => field.key)]);
  return Object.fromEntries(Object.entries(job.params || {}).filter(([key]) => !hidden.has(key)));
}
function queueInputFiles(job) {
  const files = (job.files || []).filter((file) => file.kind === "input");
  if (!job.task_flow) return files;
  const params = queueVisibleParams(job), ids = new Set();
  for (const field of job.parameters || []) {
    if (!isAttachmentField(field)) continue;
    try { for (const id of fieldAttachmentIds(field, params[field.key] || "")) ids.add(id); }
    catch { /* Invalid references never expand the displayed file scope. */ }
  }
  return files.filter((file) => ids.has(file.id));
}
function queueStepMarkup(job) {
  const flow = job.task_flow;
  if (!flow) return `<p>${job.progress}% · ${esc(job.message)}</p>${job.steps?.length ? `<p class="caption">${job.steps.filter((step) => step.done).length} / ${job.steps.length} 步已完成</p>` : ""}`;
  const label = localized(flow.current?.label) || tr("当前步骤", "Current step");
  const phase = ({ await_start: tr("等待顾客开始", "Waiting for customer to start"), input: tr("等待顾客填写", "Waiting for customer input"), display: tr("等待顾客查看结果", "Waiting for customer review"), queued: tr("等待领取", "Awaiting claim"), processing: tr("正在处理", "Processing"), ended: tr("流程已结束", "Flow ended") })[flow.phase] || "";
  const deadline = Number(flow.deadline), now = Number(flow.server_time) || Date.now() / 1000;
  const remaining = flow.deadline != null && Number.isFinite(deadline) ? Math.max(0, Math.ceil(deadline - now)) : null;
  const timing = remaining === null ? "" : `<p class="caption">${remaining ? tr(`本步剩余约 ${remaining} 秒`, `About ${remaining} seconds left for this step`) : tr("本步时间已到，请刷新状态", "Step time is up. Refresh its status.")}</p>`;
  return `<p>${esc(label)} · ${esc(phase)}</p>${timing}${job.message ? `<p>${esc(job.message)}</p>` : ""}${job.protected_fields?.length ? `<p class="caption">${tr("本步包含临时敏感输入，通过已绑定的 CLI 执行器读取。", "This step has temporary sensitive input. Read it through the bound CLI executor.")}</p>` : ""}`;
}
async function renderJobs(filter = "", requestedProductId = queueProductId, requestedView = queueView) {
  const loadId = ++queueLoadId;
  const pathname = location.pathname;
  const authority = managementAuthority(), requestOptions = managementOptions();
  const view = ["active", "processed", "all"].includes(requestedView) ? requestedView : "active";
  queueView = view;
  document.querySelectorAll("[name=job]").forEach((node) => (node.checked = false));
  if ($("#all")) $("#all").checked = false;
  if ($("#batch-form")) $("#batch-form").innerHTML = "";
  if ($("#queue-bulk")) $("#queue-bulk").hidden = true;
  const active = () =>
    loadId === queueLoadId && managementAuthority() === authority && authStatus.session_id === requestOptions.expectedSessionId && location.pathname === pathname && tab === "jobs";
  const available = await api("/manage/products?view=history", undefined, "GET", requestOptions);
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
  if (role === "staff" && productId === managedProductId) {
    managedProductPurged = selectedProduct.purged === true || selectedProduct.purged_at != null;
    managedProductDeleted = managedProductPurged || selectedProduct.deleted === true || selectedProduct.deleted_at != null;
  }
  const query = new URLSearchParams({ product_id: productId, view });
  if (filter) query.set("state", filter);
  const rows = await api("/manage/jobs?" + query, undefined, "GET", requestOptions);
  if (!active()) return;
  const manual = selectedProduct.mode === "manual";
  const canProcess = manual && permitted("queue.process");
  let batchDialogGeneration = 0;
  const dialogCurrent = (generation) => active() && generation === batchDialogGeneration;
  const closeBatchDialog = () => { batchDialogGeneration++; $("#batch-form").innerHTML = ""; };
  const taskInputs = (j) => `<details class="queue-input"><summary>${tr("查看需求与附件", "View requirements and attachments")}${queueInputFiles(j).length ? ` · ${queueInputFiles(j).length}` : ""}</summary>${j.revision?.is_revision ? `<div class="revision-feedback"><h4>${tr(`第 ${j.revision.current} 轮修改建议`, `Feedback for revision ${j.revision.current}`)}</h4><p>${esc(j.revision.message || "")}</p></div>` : ""}${j.card_attributes && Object.keys(j.card_attributes).length ? `<details><summary>${tr("卡密绑定属性", "Frozen code attributes")}</summary><pre>${esc(JSON.stringify(j.card_attributes, null, 2))}</pre></details>` : ""}<pre>${esc(JSON.stringify(queueVisibleParams(j), null, 2))}</pre>${queueInputFiles(j).map((file) => `<p class="queue-input-file"><button type="button" class="secondary" data-management-download="${esc(file.id)}">${esc(file.filename)}</button><span class="caption">${Math.ceil(file.size / 1024)} KiB</span></p>`).join("")}</details>`;
  const taskWorker = (j) => j.processing_worker && window.ExtoreWorkerIdentity ? window.ExtoreWorkerIdentity.markup(j.processing_worker, { language: lang, compact: true }) : "";
  $("#workspace").innerHTML = `<section class="queue-workspace">
    <header class="queue-page-heading"><div><h2>${tr("处理队列", "Processing queue")}</h2><p class="caption">${manual ? tr("先领取任务，再更新进度或交付。", "Claim a task before updating progress or delivering results.") : tr("商品处理器自动执行；这里查看处理记录。", "The product processor runs automatically. View its activity here.")}</p></div><button id="copy-queue-ai" class="secondary">${tr("复制 AI 提示词", "Copy AI prompt")}</button></header>
    <div class="queue-filter-bar"><div class="field queue-picker"><label for="queue-product">${tr("商品", "Product")}</label><select id="queue-product" ${role === "staff" ? "disabled" : ""}>${available.map((p) => `<option value="${esc(p.id)}" ${p.id === productId ? "selected" : ""}>${esc(p.name)}${p.deleted === true || p.deleted_at ? tr("（已删除）", " (deleted)") : ""}</option>`).join("")}</select></div><div class="field"><label for="job-view">${tr("任务范围", "Tasks")}</label><select id="job-view"><option value="active" ${view === "active" ? "selected" : ""}>${tr("待处理", "Pending")}</option><option value="processed" ${view === "processed" ? "selected" : ""}>${tr("已处理", "Processed")}</option><option value="all" ${view === "all" ? "selected" : ""}>${tr("全部", "All")}</option></select></div><div class="field"><label for="job-state">${tr("状态", "State")}</label><select id="job-state"><option value="">${tr("全部状态", "All states")}</option>${Object.keys(states).map((key) => `<option value="${key}" ${filter === key ? "selected" : ""}>${esc(lang === "en" ? stateEn[key] : states[key])}</option>`).join("")}</select></div><button id="refresh" class="secondary">${tr("刷新", "Refresh")}</button></div>
    ${role === "staff" ? `<p class="caption queue-binding-note">${tr(`CLI 剩余绑定次数：${managementRemainingCLIUses}。`, `CLI bindings remaining: ${managementRemainingCLIUses}. `)}${managementRemainingCLIUses ? tr("复制提示词后，在网页核对设备码并授权。", "After copying the prompt, review the device code and approve it in the browser.") : tr("请使用已绑定的 CLI 设备和原来的配置文件。", "Use the already-bound CLI device and its existing profile.")}</p>` : ""}<div id="queue-ai-prompt"></div>
    ${rows.length ? `<div class="queue-list-heading"><label class="queue-select-all"><input id="all" type="checkbox" aria-label="${tr("选择当前商品的全部任务", "Select all tasks in this product")}">${tr("选择本页", "Select page")}</label><p class="caption">${tr(`本页 ${rows.length} 个任务`, `${rows.length} tasks on this page`)}</p></div>${canProcess || role === "admin" ? `<div id="queue-bulk" class="queue-bulk" hidden><p id="queue-selected-count" role="status"></p><div class="queue-bulk-actions">${canProcess ? `<button id="claim" disabled>${tr("领取任务", "Claim tasks")}</button><button id="progress-update" class="secondary" disabled>${tr("更新进度", "Update progress")}</button><button id="complete" class="secondary" disabled>${tr("提交交付", "Deliver results")}</button>` : ""}<details class="queue-more"><summary>${tr("更多操作", "More actions")}</summary><div class="queue-more-actions">${canProcess ? `<button id="request-changes" class="secondary" disabled>${tr("需要重试", "Request retry")}</button><button id="fail" class="secondary" disabled>${tr("标记失败", "Mark failed")}</button><button id="reject" class="danger" disabled>${tr("永久拒绝", "Permanently reject")}</button>` : ""}${role === "admin" ? `<button id="release" class="secondary" disabled>${tr("核实后允许重试", "Allow retry after verification")}</button>` : ""}</div></details></div><p class="caption">${tr("只操作当前商品。批量交付使用相同内容；有附件的任务请逐个交付。", "Current product only. Bulk delivery uses identical content; deliver tasks with attachments individually.")}</p></div>` : ""}<ol class="queue-task-list">${rows.map((j) => `<li class="queue-task"><div class="queue-task-heading"><label class="queue-task-select"><input type="checkbox" name="job" value="${esc(j.id)}" aria-label="${esc(tr("选择任务 ", "Select task ") + j.id)}"><span class="mono">${esc(j.id)}</span></label>${status(j)}</div><div class="queue-task-meta">${j.variant?.name ? `<span>${esc(j.variant.name)}</span>` : ""}${j.state === "queued" && j.queue_position ? `<span>${tr(`商品内排第 ${j.queue_position} 位`, `Position ${j.queue_position} in this product`)}</span>` : ""}<span>${tr(`第 ${j.attempt} 次尝试`, `Attempt ${j.attempt}`)}</span></div>${taskWorker(j)}<div class="queue-task-body"><div class="queue-task-progress">${queueStepMarkup(j)}</div>${taskInputs(j)}</div></li>`).join("")}</ol>` : `<div class="queue-empty"><h3>${view === "processed" ? tr("暂无已处理任务", "No processed tasks") : filter ? tr("没有符合条件的任务", "No matching tasks") : tr("队列暂时空着", "The queue is empty")}</h3><p>${filter ? tr("可以切换状态，或刷新查看新任务。", "Change the state filter or refresh for new tasks.") : manual ? tr("有新任务时会出现在这里。也可以把上方提示词交给 AI，用 CLI 等待任务。", "New tasks will appear here. Give the prompt above to your AI to wait for work in the CLI.") : tr("自动处理记录会出现在这里。", "Automatic processing activity will appear here.")}</p></div>`}
    <div id="batch-form"></div><div id="error" class="error" role="alert"></div></section>`;
  window.ExtoreWebMCP?.refresh();
  bindManagementDownloads(rows.flatMap(queueInputFiles), active, requestOptions);
  on("#copy-queue-ai", async () => {
    if (!active()) return;
    const authorization = { origin: location.origin,
      reuseDevice: role === "staff" && managementRemainingCLIUses <= 0 };
    await copyCLIPrompt({ ...authorization, product: selectedProduct, existingLink: role === "staff", permissions: role === "staff" ? permissions : [], reuseDevice: role === "staff" && managementRemainingCLIUses === 0 }, $("#queue-ai-prompt"), active);
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
  const updateSelection = () => {
    if (!active()) return;
    const checks = [...document.querySelectorAll("[name=job]")];
    const selected = checks.filter((node) => node.checked).map((node) => rows.find((row) => row.id === node.value));
    const valid = selected.length > 0 && selected.every(Boolean);
    if ($("#queue-bulk")) $("#queue-bulk").hidden = !valid;
    if ($("#queue-selected-count")) $("#queue-selected-count").textContent = tr(`已选择 ${selected.length} 个任务`, `${selected.length} tasks selected`);
    if ($("#all")) { $("#all").checked = !!checks.length && selected.length === checks.length; $("#all").indeterminate = selected.length > 0 && selected.length < checks.length; }
    const ownProcessing = valid && selected.every((row) => row.state === "processing" && row.claimed_by === currentManagementActor());
    for (const selector of ["#progress-update", "#complete", "#fail", "#request-changes", "#reject"]) if ($(selector)) $(selector).disabled = !ownProcessing;
    if ($("#claim")) $("#claim").disabled = !valid || selected.some((row) => row.state !== "queued");
    if ($("#release")) $("#release").disabled = !valid || selected.some((row) => row.state !== "failed");
  };
  $("#all")?.addEventListener("change", () => {
    document.querySelectorAll("[name=job]").forEach((node) => (node.checked = $("#all").checked));
    updateSelection();
  });
  document.querySelectorAll("[name=job]").forEach((node) => node.addEventListener("change", updateSelection));
  updateSelection();
  const ids = () => {
    const values = [...document.querySelectorAll("[name=job]:checked")].map(
      (c) => c.value,
    );
    if (!values.length) throw new Error("请先选择当前商品的任务");
    return values;
  };
  const executeBatch = (body, snapshots = rows) => {
    if (!active()) throw new Error("商品队列已切换，请重新选择任务。");
    const selected = body.ids.map((id) => snapshots.find((row) => row.id === id && row.product_id === productId));
    if (selected.some((row) => !row)) throw new Error("任务不属于当前商品队列，请刷新后重新选择。");
    const flowScopes = Object.fromEntries(selected.filter((row) => row.task_flow || row.revision?.current > 0).map((row) => {
      if (body.action === "retry" || !row.task_flow) {
        if (!Number.isSafeInteger(row.attempt) || row.attempt < 1) throw new Error("任务尝试信息无效，请刷新队列。");
        return [row.id, { attempt: row.attempt }];
      }
      return [row.id, managementFlowScope(row)];
    }));
    return api("/manage/batch", { product_id: productId, ...body, ...(Object.keys(flowScopes).length ? { flow_scopes: flowScopes } : {}) }, "POST", requestOptions);
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
    const actor = currentManagementActor();
    const selectedRows = rows.filter((row) => selected.includes(row.id));
    if (!canProcess || selectedRows.length !== selected.length || selectedRows.some((row) => row.state !== "processing" || row.claimed_by !== actor))
      throw new Error("只能处理你已领取的处理中任务。");
    const generation = ++batchDialogGeneration;
    const rejecting = action === "reject";
    $("#batch-form").innerHTML = `<div class="panel"><h2>${rejecting ? "永久拒绝" : "需要重试"} · ${selected.length} 个任务</h2>${rejecting ? "" : '<div class="field"><label for="retry-reason-kind">原因类型</label><select id="retry-reason-kind"><option value="customer_input">需要补充信息</option><option value="external">外部服务问题</option><option value="processor">程序问题</option></select></div><div class="field"><label for="retry-mode">顾客重试方式</label><select id="retry-mode"><option value="revise">修改资料后重新提交</option><option value="reuse">允许使用原资料重试</option></select><p class="caption">选择复用不会自动重试，仍须由顾客明确点击提交。</p></div>'}<div class="field"><label for="disposition-reason">${rejecting ? "拒绝原因" : "需要重试的原因"} *</label><textarea id="disposition-reason" required maxlength="1000"></textarea></div><p class="caption">${rejecting ? "顾客将看到原因，这张卡密不能再次提交。" : "顾客将看到原因，并可保留已有信息后重新提交。"}</p><div class="actions"><button id="disposition-submit" class="${rejecting ? "danger" : ""}">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", closeBatchDialog);
    on("#disposition-submit", async () => {
      if (!dialogCurrent(generation)) return;
      const reason = $("#disposition-reason").value.trim();
      const kind = !rejecting && ($("#retry-reason-kind")?.value || "customer_input");
      const retryMode = !rejecting && ($("#retry-mode")?.value || "revise");
      const message = reason;
      if (!reason || message.length > 1000) throw new Error("请填写原因，最多 1000 字。");
      if (rejecting && !confirm("永久拒绝这些任务并让卡密无法再次提交？顾客会看到拒绝原因。")) return;
      await executeBatch({ ids: selected, action, message, ...(!rejecting ? { reason_type: kind, retry_mode: retryMode } : {}) });
      if (dialogCurrent(generation)) await refreshQueue();
    });
  }
  on("#request-changes", () => disposition("request_retry"));
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
      if (selected.length > 1 && outputFields.some(isAttachmentField))
        throw new Error("包含交付文件的任务请逐个完成，文件只属于自己的任务。");
      if (outputFields.some(isAttachmentField)) {
        const query = new URLSearchParams({ product_id: productId, job_id: selected[0], limit: "1" });
        const fresh = await api("/manage/jobs?" + query);
        if (!dialogCurrent(generation)) return;
        const current = fresh.find((row) => row.id === selected[0] && row.product_id === productId);
        if (!current) throw new Error("任务不属于当前商品队列。");
        if (!sameManagementStep(selectedRows[0], current)) throw new Error("任务尝试或处理步骤已改变，请刷新队列后重新选择。");
        selectedRows = [current];
        outputFields = current.outputs || selectedProduct.outputs || [];
      }
    }
    if (!dialogCurrent(generation)) return;
    const uploadedOutputs = (field) => (selectedRows[0]?.files || []).filter((file) =>
      file.job_id === selected[0] && file.kind === "output" && file.field_key === field.key &&
      !file.consumed && file.available !== false && (file.attempt == null || file.attempt === selectedRows[0].attempt) &&
      (!selectedRows[0].task_flow || file.flow_epoch === selectedRows[0].flow_epoch));
    const existingOutputIds = (field, index) => field.type === "images"
      ? [...document.querySelectorAll(`[name="batch-uploaded-${index}"]`)].filter((input) => input.checked).map((input) => input.value)
      : [$("#batch-uploaded-" + index)?.value || ""].filter(Boolean);
    const fileField = (field, index) => {
      const attachments = uploadedOutputs(field);
      const choices = !attachments.length ? "" : field.type === "images"
        ? `<fieldset class="progress-checks"><legend>已上传图片（最多 ${field.max_items || 10} 张）</legend>${attachments.map((file) => `<label><input type="checkbox" name="batch-uploaded-${index}" value="${esc(file.id)}">${esc(file.filename)} · ${Math.ceil(file.size / 1024)} KiB</label>`).join("")}</fieldset>`
        : `<label for="batch-uploaded-${index}">已上传附件</label><select id="batch-uploaded-${index}"><option value="">选择已上传附件，或选择新文件</option>${attachments.map((file) => `<option value="${esc(file.id)}">${esc(file.filename)} · ${Math.ceil(file.size / 1024)} KiB</option>`).join("")}</select>`;
      return `${choices}${attachments.length ? `<p id="batch-uploaded-id-${index}" class="caption mono"></p>${attachments.map((file) => `<p class="caption"><button type="button" class="secondary" data-management-download="${esc(file.id)}">检查附件：${esc(file.filename)}</button> · ${esc(file.id)}</p>`).join("")}` : ""}${richFieldControl("batch-output-" + index, field)}<p class="caption">附件只交付给此任务。选择新附件将替代已勾选的附件。</p>`;
    };
    $("#batch-form").innerHTML =
      `<div class="panel"><h2>${action === "succeed" ? "批量完成" : "标记失败"} · ${esc(selectedProduct.name)} · ${selected.length} 个任务</h2>${textarea("batch-message", "处理说明")}${action === "succeed" ? outputFields.map((output, i) => `<div class="field"><label for="batch-output-${i}">${esc(localized(output.label))}${output.required ? " *" : ""}</label>${isAttachmentField(output) ? fileField(output, i) : richFieldControl("batch-output-" + i, output, "", { maximum: 100000 })}</div>${localized(output.description) ? `<details ${output.collapsed ? "" : "open"}><summary>交付说明</summary><div class="markdown">${md(localized(output.description))}</div></details>` : ""}`).join("") || '<p class="caption">此商品只交付服务状态，无需填写内容。</p>' : '<div class="checks"><label><input id="batch-retry" type="checkbox">已确认未交付，允许顾客重试</label></div>'}<div class="actions"><button id="batch-submit">确认提交</button><button id="batch-cancel" class="secondary">取消</button></div></div>`;
    on("#batch-cancel", closeBatchDialog);
    bindManagementDownloads(selectedRows.flatMap((row) => row.files || []), () => dialogCurrent(generation), requestOptions);
    for (const [index, field] of outputFields.entries()) {
      if (action !== "succeed" || !isAttachmentField(field)) continue;
      const input = $("#batch-output-" + index);
      const existing = $("#batch-uploaded-" + index);
      const updateSelection = () => {
        const ids = existingOutputIds(field, index);
        input.required = !!field.required && !ids.length;
        const label = $("#batch-uploaded-id-" + index);
        if (label) label.textContent = ids.length ? "已选择附件 ID：" + ids.join(", ") : "";
      };
      existing?.addEventListener("change", updateSelection);
      document.querySelectorAll(`[name="batch-uploaded-${index}"]`).forEach((choice) => choice.addEventListener("change", updateSelection));
      input.addEventListener("change", updateSelection);
      updateSelection();
    }
    if (action === "succeed" && outputFields.some(isAttachmentField)) void uploadFileLimit();
    on("#batch-submit", async () => {
      if (!dialogCurrent(generation)) return;
      const output = {};
      const message = $("#batch-message").value;
      if (action === "succeed") {
        for (const [i, definition] of outputFields.entries()) {
          const input = $("#batch-output-" + i);
          if (!input.reportValidity()) return;
          if (isAttachmentField(definition)) {
            const existing = existingOutputIds(definition, i);
            if (existing.some((id) => !uploadedOutputs(definition).some((candidate) => candidate.id === id)))
              throw new Error("附件不属于此任务、字段或当前尝试，请刷新后重新选择。");
            const retained = definition.type === "images" ? JSON.stringify(existing) : existing[0] || "";
            const flowScope = managementFlowScope(selectedRows[0]);
            if (!Number.isSafeInteger(selectedRows[0].attempt) || selectedRows[0].attempt < 1) throw new Error("任务尝试信息无效，请刷新队列。");
            output[definition.key] = await uploadFieldFiles(input, definition, retained, "/manage/files/upload", { job_id: selected[0], attempt: selectedRows[0].attempt, ...(flowScope ? { flow_epoch: flowScope.flow_epoch, action_id: flowScope.action_id } : {}) }, { ...requestOptions, isCurrent: () => dialogCurrent(generation) });
          } else output[definition.key] = input.value;
          if (!dialogCurrent(generation)) return;
        }
      }
      if (action === "succeed" && selectedRows[0]?.task_flow && outputFields.some(isAttachmentField)) {
        const fresh = await selectedJobForFile({ product_id: productId, job_id: selected[0] }, requestOptions);
        if (!dialogCurrent(generation)) return;
        if (!sameManagementStep(selectedRows[0], fresh)) throw new Error("处理步骤已改变，附件尚未提交，请刷新队列。");
      }
      await executeBatch({
        ids: selected,
        action,
        message,
        output: action === "succeed" ? output : undefined,
        retryable: $("#batch-retry")?.checked || false,
      }, selectedRows);
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
  const available = await api((role === "staff" ? "/manage" : "/admin") + "/products?view=history");
  if (
    loadId !== queueLoadId ||
    pathname !== location.pathname ||
    tab !== "cards"
  )
    return;
  return window.ExtoreCards.render({
    api,
    workspace: $("#workspace"),
    products: available,
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
async function renderStaff(requestedView = linkView) {
  const linkLoad = ++linkLoadId;
  linkView = ["active", "history", "all"].includes(requestedView) ? requestedView : "active";
  const selectedView = linkView;
  const generation = queueLoadId;
  const pathname = location.pathname;
  const renderRole = role;
  const authority = managementAuthority();
  const requestOptions = managementOptions();
  const active = () =>
    queueLoadId === generation && linkLoad === linkLoadId && selectedView === linkView &&
    location.pathname === pathname &&
    role === renderRole && managementAuthority() === authority &&
    tab === "staff";
  const endpoint = renderRole === "staff" ? "/manage/links" : "/admin/staff";
  const availableProducts =
    renderRole === "staff" ? await api("/manage/products", undefined, "GET", requestOptions) : products;
  if (!active()) return;
  const returned = await api(endpoint + (selectedView === "active" ? "" : "?" + new URLSearchParams({ view: selectedView })), undefined, "GET", requestOptions);
  if (!active()) return;
  const links = returned.filter((link) => selectedView === "all" || !link.archived && (selectedView === "active" ? !link.revoked && link.expires * 1000 > Date.now() : link.revoked || link.expires * 1000 <= Date.now()));
  products = availableProducts;
  const availablePermissions = Object.keys(permissionLabels).filter(permitted);
  const remainingDays = managementExpires
    ? Math.max(0, (managementExpires * 1000 - Date.now()) / 86400000)
    : 90;
  const defaultDays =
    role === "staff" ? Math.min(7, Math.floor(remainingDays * 100) / 100) : 7;
  $("#workspace").innerHTML =
    `<h2>商品管理链接</h2><p class="caption">一条链接只授权一个商品。可以分别授予队列、商品配置、卡密等权限；店长可获得该商品的完整管理权限。默认可绑定浏览器一次、CLI 一次；已登录的会话可继续使用。链接是登录凭证，请私下交给管理者。</p>${role === "staff" ? '<p class="caption">你只能创建权限比自己更小的下级链接，有效期也不能超过自己的链接。</p>' : ""}<form id="form"><div class="grid">${field("staff-name", "管理者或链接名称")}<div class="field"><label for="staff-product">授权商品</label><select id="staff-product" ${role === "staff" ? "disabled" : ""}>${productOptions()}</select></div>${field("staff-days", "有效天数", defaultDays, "number")}${field("staff-max-uses", "浏览器绑定次数", 1, "number")}${field("staff-max-cli-uses", "CLI 绑定次数", 1, "number")}</div><fieldset class="permission-fields"><legend>权限范围</legend><div class="permission-presets">${availablePermissions.includes("queue.monitor") ? `<button type="button" id="preset-monitor" class="secondary">${tr("只看进度", "Progress only")}</button>` : ""}<button type="button" id="preset-view" class="secondary">只看队列</button><button type="button" id="preset-process" class="secondary">处理任务</button>${role === "admin" ? '<button type="button" id="preset-manager" class="secondary">店长：完全管理商品</button>' : ""}</div><div class="permission-grid">${availablePermissions.map((key) => `<label><input type="checkbox" name="link-permission" value="${key}" ${["queue.view", "queue.process"].includes(key) ? "checked" : ""}>${permissionLabels[key]}</label>`).join("")}</div><p class="caption">“创建下级管理链接”允许继续委派。下级必须少至少一项权限，不能扩大商品范围或有效期。撤销上级链接会同时撤销全部下级。</p></fieldset><button type="submit" class="full" ${products.length ? "" : "disabled"}>创建管理链接</button><div id="error" class="error" role="alert"></div></form><div id="staff-link"></div><div class="form-divider toolbar"><div class="field"><label for="links-view">链接记录</label><select id="links-view"><option value="active" ${selectedView === "active" ? "selected" : ""}>活动链接</option><option value="history" ${selectedView === "history" ? "selected" : ""}>撤销与过期历史</option><option value="all" ${selectedView === "all" ? "selected" : ""}>全部（含已清理）</option></select></div><button id="links-cleanup-preview" class="secondary">预览清理旧链接</button></div><div id="links-cleanup"></div><div id="maintenance-confirmation"></div><div class="form-divider table-wrap"><table><thead><tr><th>管理链接</th><th>商品</th><th>权限</th><th>有效期</th><th>登录次数</th><th></th></tr></thead><tbody>${links.map((link) => `<tr><td>${esc(link.name)}${link.parent_id ? '<div class="caption">下级链接</div>' : ""}</td><td>${esc(products.find((p) => p.id === link.product_id)?.name || link.product_id)}</td><td>${(link.permissions || []).map((key) => esc(permissionLabels[key] || key)).join("<br>")}</td><td>${link.revoked ? "已撤销" : new Date(link.expires * 1000).toLocaleString()}</td><td>浏览器 ${link.uses || 0} / ${link.max_uses || 1}<p class="caption">剩余 ${link.remaining_uses ?? Math.max(0, (link.max_uses || 1) - (link.uses || 0))} 次</p>CLI ${link.cli_uses || 0} / ${link.max_cli_uses || 1}<p class="caption">剩余 ${link.remaining_cli_uses ?? Math.max(0, (link.max_cli_uses || 1) - (link.cli_uses || 0))} 次</p></td><td>${!link.revoked ? `<button data-revoke="${esc(link.id)}" class="danger">撤销</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  on("#links-view", () => renderStaff($("#links-view").value), "change");
  on("#links-cleanup-preview", async () => {
    if (!active()) return;
    const cleanupPath = renderRole === "staff" ? "/manage/links/cleanup" : "/admin/maintenance/cleanup";
    const body = { dry_run: true, limit: 100, ...(renderRole === "staff" ? { product_id: managedProductId } : { areas: ["links"] }) };
    const preview = await api(cleanupPath, body, "POST");
    if (!active()) return;
    $("#links-cleanup").innerHTML = `<section class="panel"><p>符合保留期限的旧链接：${Number(preview.eligible?.links) || 0} 条。清理保留撤销记录与祖先链，活动链接不受影响。</p><button id="links-cleanup-run" class="danger" ${preview.eligible?.links ? "" : "disabled"}>确认清理（本次最多 100 条）</button></section>`;
    on("#links-cleanup-run", async () => {
      if (!active()) return;
      const execute = async () => { if (!active()) return; await api(cleanupPath, { ...body, dry_run: false }, "POST"); if (active()) await renderStaff(selectedView); };
      if (renderRole === "admin") confirmManagementAction(execute, "确认清理旧链接", active);
      else if (confirm("清理授权商品内达到保留期限的旧下级链接？撤销与祖先关系会保留。")) await execute();
    });
  });
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
  on("#preset-monitor", () => choosePermissions(["queue.monitor"]));
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
    const creationView = linkView;
    const active = () =>
      queueLoadId === creationGeneration &&
      location.pathname === creationPath &&
      role === creationRole && linkView === creationView &&
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
    on("#copy-link-ai", () => copyCLIPrompt({ origin: new URL(result.url).origin, link: result.url, existingLink: true, product: productDefinition, permissions: selected, expires: result.expires }, $("#link-ai-prompt"), () => active() && input.isConnected));
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
function confirmManagementAction(handler, title, isCurrent) {
  const root = $("#maintenance-confirmation");
  if (!root || !isCurrent()) return;
  accountView?.dispose();
  accountView = window.ExtoreAccount.mount({ root, api, auth: authStatus, mode: "confirm", passkey, onAuth: acceptAuth, language: () => lang, isCurrent });
  accountView.confirmFresh(handler, title);
}
async function renderEvents() {
  const load = ++eventLoadId, generation = queueLoadId, pathname = location.pathname, viewRole = role;
  const authority = managementAuthority(), requestOptions = managementOptions();
  const active = () => load === eventLoadId && generation === queueLoadId && pathname === location.pathname && role === viewRole && managementAuthority() === authority && tab === "events";
  const endpoint = viewRole === "staff" ? "/manage/events" : "/admin/events";
  const owner = window.ExtoreAccount?.rootScope(authStatus) || window.ExtoreAccount?.shopScope(authStatus);
  const [rows, maintenance] = await Promise.all([api(endpoint, undefined, "GET", requestOptions), owner ? api("/admin/maintenance", undefined, "GET", requestOptions) : Promise.resolve(null)]);
  if (!active()) return;
  const policy = maintenance?.policy || {};
  $("#workspace").innerHTML = `<div class="section-head"><h2>事件记录</h2><button id="refresh" class="secondary">刷新</button></div><p class="caption">投递失败最多自动重试 8 次。外部平台须按事件 ID 去重。等待投递的事件不会被清理。</p>${owner ? `<details class="panel"><summary>保留期限与清理</summary><p class="caption">当前范围：${esc(maintenance.shop_id || (authStatus.superadmin ? "全部店铺" : authStatus.shop_id))}。已完成事件与未配置投递的事件按事件期限清理，失败事件单独保留。</p><form id="maintenance-policy-form"><div class="grid">${field("retention-events", "完成事件保留天数", policy.event_retention_days || 30, "number")}${field("retention-dead", "失败投递保留天数", policy.dead_letter_retention_days || 90, "number")}${field("retention-audit", "审计记录保留天数", policy.audit_retention_days || 180, "number")}${field("retention-links", "旧链接保留天数", policy.link_retention_days || 90, "number")}</div><label><input id="retention-enabled" type="checkbox" ${policy.enabled !== false ? "checked" : ""}>按保留期限自动清理</label><div class="actions"><button type="submit">验证身份并保存期限</button><button id="events-cleanup-preview" type="button" class="secondary">预览清理旧事件</button></div></form><div id="events-cleanup"></div></details>` : ""}<div id="maintenance-confirmation"></div><div class="table-wrap"><table><thead><tr><th>事件</th><th>时间</th><th>Webhook</th><th>尝试</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.type)}<div class="mono muted">${esc(r.id)}</div></td><td>${esc(new Date(r.created * 1000).toLocaleString())}</td><td>${esc(r.webhook_state || "未配置")}<div class="caption">${esc(r.error)}</div></td><td>${esc(r.attempts ?? "—")}</td><td>${r.webhook_state === "dead" ? `<button data-event="${esc(r.id)}" class="secondary">重新投递</button>` : ""}</td></tr>`).join("")}</tbody></table></div><div id="error" class="error" role="alert"></div>`;
  on("#refresh", renderEvents);
  on("#maintenance-policy-form", () => {
    if (!active()) return;
    const body = { enabled: $("#retention-enabled").checked, event_retention_days: Number($("#retention-events").value), dead_letter_retention_days: Number($("#retention-dead").value), audit_retention_days: Number($("#retention-audit").value), link_retention_days: Number($("#retention-links").value) };
    if (Object.entries(body).some(([key, value]) => key !== "enabled" && (!Number.isInteger(value) || value < 1 || value > 3650))) throw new Error("保留期限须为 1 至 3650 天的整数");
    if (body.audit_retention_days < 90) throw new Error("审计记录至少保留 90 天");
    confirmManagementAction(async () => { if (!active()) return; await api("/admin/maintenance/policy", body, "PUT"); if (active()) await renderEvents(); }, "确认更改记录保留期限", active);
  }, "submit");
  on("#events-cleanup-preview", async () => {
    if (!active()) return;
    const body = { areas: ["events"], dry_run: true, limit: 100 };
    const preview = await api("/admin/maintenance/cleanup", body, "POST");
    if (!active()) return;
    $("#events-cleanup").innerHTML = `<p>符合当前期限的旧事件：${Number(preview.eligible?.events) || 0} 条。待投递事件保留。</p><button id="events-cleanup-run" class="danger" ${preview.eligible?.events ? "" : "disabled"}>确认清理（本次最多 100 条）</button>`;
    on("#events-cleanup-run", () => { if (!active()) return; confirmManagementAction(async () => { if (!active()) return; await api("/admin/maintenance/cleanup", { ...body, dry_run: false }, "POST"); if (active()) await renderEvents(); }, "确认清理旧事件", active); });
  });
  document.querySelectorAll("[data-event]").forEach((button) => button.addEventListener("click", () => perform(async () => {
    if (!active()) return;
    await api(endpoint + "/" + encodeURIComponent(button.dataset.event) + "/retry", {});
    if (active()) await renderEvents();
  })));
}
async function renderSessions() {
  pipelineAuthView?.dispose();
  pipelineAuthView = null;
  const authority = managementAuthority();
  const requestOptions = managementOptions();
  const generation = queueLoadId;
  const viewRole = role;
  const pathname = location.pathname;
  const active = () => generation === queueLoadId && role === viewRole && managementAuthority() === authority && tab === "sessions" && location.pathname === pathname;
  const prefix = role === "admin" ? "/admin" : "/manage";
  const [sessions, entries, devices, ownerDevices] = await Promise.all([
    api(prefix + "/sessions", undefined, "GET", requestOptions), api(prefix + "/audit?limit=200", undefined, "GET", requestOptions), api(prefix + "/cli-devices", undefined, "GET", requestOptions),
    viewRole === "admin" ? api("/admin/cli-owner-devices", undefined, "GET", requestOptions) : Promise.resolve([]),
  ]);
  if (!active()) return;
  const date = (value) => value ? new Date(value * 1000).toLocaleString() : "—";
  const ownerDeviceMarkup = viewRole === "admin" ? `<h2 class="form-divider">${tr("商家 CLI 设备 · 全店管理", "Merchant CLI devices · full shop access")}</h2><p class="caption">${tr("首次授权须核对设备码与指纹，再用 Passkey 批准。此类设备拥有完整商家权限，与商品管理链接相互独立。撤销会结束该设备的全部 CLI 会话，阻止续期；浏览器会话不受影响。", "First authorization requires a device-code and fingerprint review followed by Passkey approval. These devices have full merchant access, independent of product links. Revoking one ends its CLI sessions and prevents renewal; browser sessions are unaffected.")}</p>${ownerDevices.length ? `<div class="table-wrap"><table><thead><tr><th>${tr("设备 / 指纹", "Device / fingerprint")}</th><th>${tr("权限 / 到期", "Scope / expiry")}</th><th>${tr("授权 / 最近使用", "Authorized / last used")}</th><th>${tr("状态", "Status")}</th><th></th></tr></thead><tbody>${ownerDevices.map((device) => `<tr><td>${esc(device.client_name || tr("未命名商家 CLI 设备", "Unnamed merchant CLI device"))}<p class="caption mono">${esc(device.fingerprint || "—")}</p><p class="caption mono">${esc(device.id)}</p></td><td>${tr("完整商家管理权限", "Full merchant management")}<p class="caption">${date(device.expires)}</p></td><td>${date(device.created)}<p class="caption">${date(device.last_seen)}</p></td><td>${device.revoked ? tr("已撤销", "Revoked") : device.active ? tr("有效", "Active") : tr("已到期", "Expired")}</td><td>${!device.revoked ? `<button class="danger" data-revoke-owner-device="${esc(device.id)}">${tr("撤销商家设备", "Revoke merchant device")}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : `<p class="caption">${tr("暂无商家 CLI 设备。", "No merchant CLI devices yet.")}</p>`}` : "";
  const deviceMarkup = `<h2 class="form-divider">商品 CLI 设备 · 按商品授权</h2><p class="caption">撤销设备会结束它的 CLI 会话，并阻止它再次申请会话；浏览器会话不受影响。已用绑定次数不会退还。</p>${devices.length ? `<div class="table-wrap"><table><thead><tr><th>设备 / 指纹</th><th>商品 / 管理链接</th><th>绑定 / 最近使用</th><th>状态</th><th></th></tr></thead><tbody>${devices.map((device) => `<tr><td>${esc(device.client_name || "未命名 CLI 设备")}<p class="caption mono">${esc(device.fingerprint || "—")}</p><p class="caption mono">${esc(device.id)}</p></td><td>${esc(device.product_name || device.product_id)}<p class="caption">${esc(device.link_name || "商品管理链接")}</p></td><td>${date(device.created)}<p class="caption">${date(device.last_seen)}</p></td><td>${device.revoked ? "已撤销" : device.active ? "有效" : "授权已失效"}</td><td>${!device.revoked ? `<button class="danger" data-revoke-device="${esc(device.id)}">撤销设备</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : '<p class="caption">暂无 CLI 设备。</p>'}`;
  $("#workspace").innerHTML = `<div class="section-head"><div><h2>会话管理</h2><p class="caption">${role === "admin" ? "查看全店会话，按需注销单个设备。" : permitted("links.delegate") ? "查看自己的会话和下级链接的会话，只限授权商品。" : "查看与注销自己的会话。"} 链接次数耗尽不会结束已有会话。</p></div><div class="actions">${viewRole === "admin" ? `<button id="copy-owner-ai" class="secondary">${tr("复制给 AI 的完整管理提示词", "Copy full management prompt for AI")}</button>` : ""}<button id="sessions-refresh" class="secondary">刷新</button></div></div>${viewRole === "admin" ? '<div id="owner-ai-prompt"></div>' : ""}<div class="table-wrap"><table><thead><tr><th>登录来源</th><th>设备 / 地址</th><th>登录 / 最近活动</th><th>到期</th><th>状态</th><th></th></tr></thead><tbody>${sessions.map((session) => `<tr><td>${esc(session.link_name || (session.role === "admin" ? (session.channel === "cli" ? "商家 CLI · 全店管理" : "商家 Passkey") : session.role === "bootstrap" ? "首次登录" : "商品管理链接"))}${session.product_name ? `<p class="caption">${esc(session.product_name)}</p>` : ""}<p class="caption">${session.channel === "cli" ? "CLI" : "浏览器"}${session.client_name ? " · " + esc(session.client_name) : ""}</p>${session.current ? '<p class="caption">当前会话</p>' : ""}</td><td class="session-client">${esc(session.ua || "未记录设备")}<p class="caption mono">${esc(session.ip || "—")}</p>${session.device_id ? `<p class="caption mono">设备 ${esc(session.device_id)}</p>` : ""}${session.fingerprint ? `<p class="caption mono">${esc(session.fingerprint)}</p>` : ""}</td><td>${date(session.created)}<p class="caption">${date(session.last_seen)}</p></td><td>${date(session.expires)}</td><td>${session.revoked ? "已注销" : session.active ? "有效" : "已到期"}</td><td>${session.active ? `<button class="danger" data-end-session="${esc(session.id)}">${session.current ? "退出此会话" : "注销"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>${viewRole === "admin" ? '<section id="pipeline-authorizations"></section>' : ""}${ownerDeviceMarkup}${deviceMarkup}<h2 class="form-divider">访问审计</h2><p class="caption">最近 200 条登录、链接使用、委派和注销记录。记录中不包含卡密或授权凭证。</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>目标</th></tr></thead><tbody>${entries.map((entry) => `<tr><td>${date(entry.created)}</td><td>${esc(entry.action)}${entry.channel ? `<p class="caption">${entry.channel === "cli" ? "CLI" : "浏览器"}${entry.client_name ? " · " + esc(entry.client_name) : ""}</p>` : ""}${entry.fingerprint ? `<p class="caption mono">${esc(entry.fingerprint)}</p>` : ""}</td><td class="mono">${esc(entry.actor)}</td><td class="mono">${esc(entry.target)}</td></tr>`).join("")}</tbody></table></div><div id="error" class="error" role="alert"></div>`;
  if (viewRole === "admin" && window.ExtorePipelineAuthorizations) {
    pipelineAuthView = window.ExtorePipelineAuthorizations.mount({
      root: $("#pipeline-authorizations"), auth: authStatus, language: () => lang,
      api: (path, body, method, options = {}) => api(path, body, method, { ...managementOptions(), ...options }),
      isCurrent: active, passkey, onAuth: acceptAuth,
      copyPrompt: (options, host, current) => copyCLIPrompt({ origin: location.origin, ...options }, host, current),
    });
  }
  on("#sessions-refresh", renderSessions);
  if (viewRole === "admin") on("#copy-owner-ai", async () => {
    if (active()) await copyCLIPrompt({ owner: true, origin: location.origin }, $("#owner-ai-prompt"), active);
  });
  document.querySelectorAll("[data-revoke-owner-device]").forEach((button) => button.addEventListener("click", () => perform(async () => {
    if (!active() || viewRole !== "admin") return;
    const device = ownerDevices.find((item) => item.id === button.dataset.revokeOwnerDevice);
    if (!device || device.revoked) return;
    if (!confirm(tr("撤销这个拥有全店管理权限的商家 CLI 设备，并结束它的全部 CLI 会话？再次使用须重新通过 Passkey 授权。", "Revoke this full-access merchant CLI device and end all its CLI sessions? Passkey approval will be required to use it again."))) return;
    await api("/admin/cli-owner-devices/" + encodeURIComponent(device.id), undefined, "DELETE");
    if (active()) await renderSessions();
  }, button)));
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

function renderAccountTab() {
  const generation = queueLoadId, selected = tab, pathname = location.pathname;
  accountView = window.ExtoreAccount.mount({ root: $("#workspace"), api, auth: authStatus, mode: selected,
    language: () => lang, navigate, passkey, onAuth: acceptAuth,
    copyProcessorPrompt: (options, host, current) => copyCLIPrompt({ origin: location.origin, ...options, owner: true, processorWorkflow: true }, host, current),
    isCurrent: () => generation === queueLoadId && tab === selected && location.pathname === pathname });
}
async function renderSecurity() { renderAccountTab(); }
async function staff() {
  stopPoll();
  const generation = queueLoadId;
  const active = () => generation === queueLoadId && location.pathname === "/staff";
  if (location.hash) {
    const value = location.hash.slice(1);
    history.replaceState({}, "", "/staff");
    await api("/staff/login", { token: value });
    if (!active()) return;
  }
  const a = await api("/auth/status");
  if (!active()) return;
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
  window.ExtoreProducts?.dispose?.($("#workspace"));
  progressBoardController?.dispose(); progressBoardController = null;
  proxyConfigView?.dispose();
  proxyConfigView = null;
  window.ExtoreTaskFlow?.dispose(app);
  clearDeliveryBlobs();
  accountView?.dispose();
  accountView = null;
  ownerCliApproval?.dispose();
  ownerCliApproval = null;
  deviceCliApproval?.dispose();
  deviceCliApproval = null;
  pipelineAuthView?.dispose();
  pipelineAuthView = null;
  receiptGeneration++;
  deliveryRevealGeneration++;
  receiptRevisionRequest = null;
  receiptMotion?.dispose();
  window.ExtoreMotion?.disposeHome?.(app);
  receiptMotion = null;
  receiptViewKey = "";
  queueLoadId++;
  const generation = ++routeLoadId;
  const pathname = location.pathname;
  const hash = location.hash;
  const active = () => generation === routeLoadId && location.pathname === pathname && location.hash === hash;
  window.ExtoreMotion?.cancel(app);
  window.ExtoreMotion?.mountHome?.(app)?.dispose();
  stopPoll();
  syncPreferenceControls();
  app.innerHTML =
    '<div class="loading">' + tr("正在加载…", "Loading…") + "</div>";
  try {
    if (pathname === "/cli/owner") {
      acceptAuth({ role: null });
      currentToken = "";
      currentBatch = null;
      batchSelection = "";
      batchRetryOnly = false;
      currentProduct = null;
      currentJob = null;
      currentVariant = null;
      currentCardAttributes = {};
      currentEntitlements = null;
      queueProduct = null;
      $("#header-context").textContent = tr("商家 CLI 授权", "Merchant CLI approval");
      ownerCliApproval = window.ExtoreOwnerCli.mount({ root: app, requestId: hash.slice(1), api, language: () => lang });
      return;
    }
    const auth = await api("/auth/status");
    if (!active()) return;
    acceptAuth(auth);
    if (pathname === "/cli/device") {
      currentToken = ""; currentBatch = null; batchSelection = ""; batchRetryOnly = false;
      currentProduct = null; currentJob = null; currentVariant = null; queueProduct = null;
      $("#header-context").textContent = tr("CLI 设备授权", "CLI device authorization");
      deviceCliApproval = window.ExtoreDeviceLogin.mount({ root: app, api, auth, passkey,
        language: () => lang, isCurrent: active, onAuth: acceptAuth, navigate });
    } else if (pathname === "/account" || pathname.startsWith("/account/")) {
      const accountRoute = window.ExtoreAccount.route(pathname, hash);
      $("#header-context").textContent = tr("店铺账户", "Shop account");
      accountView = window.ExtoreAccount.mount({ root: app, api, auth, ...accountRoute,
        language: () => lang, navigate, passkey, onAuth: acceptAuth, isCurrent: active });
    } else if (location.pathname === "/admin") await admin();
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
    if (!active()) return;
    app.innerHTML = `<div class="narrow"><h1>${tr("暂时无法打开", "Unable to open")}</h1><p>${esc(e.message)}</p><button id="back" class="full">${tr("返回兑换页", "Back to redemption")}</button></div>`;
    on("#back", () => navigate("/"));
  } finally {
    if (active()) window.ExtoreWebMCP?.refresh();
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
        "/cli/owner": "owner_cli",
        "/cli/device": "cli_device",
      }[location.pathname] || "home",
    role,
    actor: currentManagementActor(),
    shopId: authStatus.shop_id ?? null,
    superadmin: authStatus.superadmin === true,
    sessionId: authStatus.session_id ?? null,
    product: currentProduct,
    flow: customerFlow(),
    currentToken,
    tab,
    cardProductId,
    queueProductId,
    queueView,
    progressBoardShopId,
    progressBoardProductId,
    progressBoardView,
    batch: Boolean(currentBatch),
    cardId: batchSelection || "",
    queueProduct,
    permissions,
    productsView,
    productDeleted: managedProductDeleted,
    productPurged: managedProductPurged,
    productId: managedProductId,
    linkExpires: managementExpires,
  }),
  actions: {
    productsPurged: async (ids, options = {}) => {
      if (options.signal?.aborted || !permitted("product.purge") || !["/admin", "/staff"].includes(location.pathname) || !Array.isArray(ids) || !ids.length || ids.length > 500 || new Set(ids).size !== ids.length || (role === "staff" && (ids.length !== 1 || ids[0] !== managedProductId))) throw new Error("商品清理操作上下文已失效。");
      products = products.filter((product) => !ids.includes(product.id));
      if (ids.includes(managedProductId)) { managedProductPurged = true; managedProductDeleted = true; }
      if (ids.includes(queueProduct?.id)) queueProduct = { ...queueProduct, deleted: true, purged: true };
      return { ok: true };
    },
    selectReceiptCard: async (cardId, options = {}) => {
      const context = receiptRequestContext(options);
      const data = await readReceipt(options);
      if (!context.active()) throw new Error("领取页面已改变，请重试。");
      const item = data.items?.find((row) => row.card_id === cardId && row.accepted !== false);
      if (!data.batch || !item) throw new Error("这张卡密不属于当前领取链接。");
      openBatchCard(item);
      return item;
    },
    uploadFile,
    readFile,
    exchange: exchangeCode,
    exchangePasted: exchangePastedCode,
    redeem: submitRedemption,
    receipt: readReceipt,
    flow: flowAction,
    retryOriginal: retryOriginalReceipt,
    reveal: revealReceipt,
    requestRevision: requestReceiptRevision,
    destroy: destroyReceipt,
    navigate,
    selectQueue: async (productId, options = {}) => {
      const available = await api(
        "/manage/products?view=history",
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
      if (location.pathname === "/account" || location.pathname.startsWith("/account/")) await start();
      else if (location.pathname === "/admin") await admin();
      else if (location.pathname === "/staff") await staff();
      else if (location.pathname === "/receipt" && currentToken)
        await readReceipt();
      else await home();
    },
  },
});
start();
