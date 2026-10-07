"use strict";

(() => {
  const mounts = new WeakMap();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const validAgentType = (value) => value === undefined || value === null || (typeof value === "string" && value.trim().length >= 1 && value.length <= 64 && !/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value));
  const digest = /^[0-9a-f]{64}$/;
  const alphabet = /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{12}$/;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const normalizeCode = (value) => String(value ?? "").trim().toUpperCase().replaceAll("-", "");
  const displayCode = (value) => normalizeCode(value).match(/.{1,4}/g)?.join("-") || "";
  const owner = (auth) => auth?.role === "admin" && ((auth.superadmin === true && auth.shop_id === null) || (auth.superadmin !== true && uuid.test(auth.shop_id || "")));
  const staff = (auth) => auth?.role === "staff" && uuid.test(auth.link_id || "") && uuid.test(auth.product_id || "") && uuid.test(auth.shop_id || "");
  const signedIn = (auth) => (owner(auth) || staff(auth)) && typeof auth.session_id === "string" && !!auth.session_id && auth.channel !== "cli";
  const authority = (auth) => JSON.stringify([auth?.role, auth?.shop_id, auth?.superadmin === true, auth?.role === "staff" ? auth.link_id : null, auth?.role === "staff" ? auth.product_id : null]);
  const permissions = {
    "queue.monitor": ["只看进度看板（不含任务内容）", "View progress board only (no task content)"],
    "queue.view": ["查看商品队列", "View product queue"],
    "queue.process": ["领取任务、更新进度与交付", "Claim tasks, update progress and deliver"],
    "queue.retry": ["允许失败任务重试", "Allow failed tasks to retry"],
    "product.edit": ["修改商品介绍与填写参数", "Edit product details and fields"],
    "fulfillment.configure": ["配置自动发货与重试规则", "Configure fulfillment and retries"],
    "cards.manage": ["发行、查看与撤销卡密", "Issue, inspect and revoke codes"],
    "events.manage": ["查看事件与重新投递", "Inspect and redeliver events"],
    "links.delegate": ["创建与撤销下级管理链接", "Create and revoke delegated links"],
    "product.delete": ["删除与恢复商品（保留旧卡密和任务）", "Delete and restore products (preserve existing codes and tasks)"],
    "product.purge": ["彻底删除商品与清空回收站（不可恢复商品）", "Permanently remove products and empty trash (products cannot be restored)"],
  };

  function mount({ root, api, auth = {}, isCurrent = () => true, passkey, onAuth = () => {}, navigate = (url) => { window.location.href = url; }, language = "zh-CN" } = {}) {
    if (!root?.querySelector || typeof api !== "function") throw new TypeError("Device authorization requires a root and API");
    mounts.get(root)?.dispose();
    let disposed = false, busy = false, deciding = false, revision = 0, code = "", review = null, account = null, state = "code";
    const controller = new AbortController();
    const active = () => !disposed && root.isConnected !== false && isCurrent();
    const $ = (selector) => root.querySelector(selector);
    const tr = (cn, en) => String(typeof language === "function" ? language() : language).toLowerCase().startsWith("en") ? en : cn;
    const date = (seconds) => new Date(seconds * 1000).toLocaleString(tr("zh-CN", "en-GB"));
    const request = (path, body, method = body ? "POST" : "GET") => api(path, body, method, { signal: controller.signal, expectedScope: auth.shop_id || (owner(auth) && auth.superadmin === true ? "platform" : undefined), expectedSessionId: auth.session_id });
    const setError = (message) => { if (active() && $("#device-error")) $("#device-error").textContent = message; };
    const stopAccount = () => { account?.dispose(); account = null; };
    const page = (body, message = "") => {
      if (!active()) return;
      stopAccount(); deciding = false;
      revision++;
      root.innerHTML = `<div class="narrow device-login"><div class="section-head"><h1>${tr("授权 CLI 设备", "Authorize a CLI device")}</h1><a href="/">${tr("兑换与领取", "Redeem and collect")}</a></div>${body}<p id="device-error" class="error" role="alert">${esc(message)}</p></div>`;
    };
    const bind = (selector, event, handler) => $(selector)?.addEventListener(event, (e) => { e.preventDefault(); return handler(e); });
    const controls = (disabled) => {
      for (const selector of ["#device-review", "#device-select-review", "#device-approve", "#device-deny", "#device-back", "#device-scope", "#device-refresh", "#device-signin", "#device-code"]) {
        const node = $(selector); if (node) node.disabled = disabled;
      }
      root.querySelectorAll?.("[data-device-selection]").forEach((node) => { node.disabled = disabled || node.dataset?.deviceFixed === "true"; });
    };
    const errorMessage = (error) => {
      const message = String(error?.message || "");
      if (/Shop owner authorization scope required|店主.*批准|店主.*审批/i.test(message)) return tr("这次申请需要店主批准。请登录对应店铺账号，或让店主在自己的浏览器输入设备码。", "This request needs shop owner approval. Sign in to the correct shop, or ask its owner to enter the device code in their browser.");
      if (error?.name === "NotAllowedError" || error?.name === "AbortError") return tr("验证已取消，请确认 CLI 仍在等待后重试。", "Verification cancelled. Retry while the CLI is still waiting.");
      if (/频繁|频率|too many|rate.limit|throttl/i.test(message)) return tr("查询太频繁，请稍后重试。", "Too many lookups. Try again shortly.");
      if (/过期|expired/i.test(message)) return tr("设备码已过期，请让 CLI 重新申请。", "The device code expired. Create a new request in the CLI.");
      if (/不存在|未找到|not.found|invalid.*code|无效.*码/i.test(message)) return tr("没有找到这个设备码，请核对 CLI 中的完整代码。", "Device code not found. Check the complete code in your CLI.");
      if (/余|次数|quota|remaining/i.test(message)) return tr("这个管理链接没有剩余的 CLI 绑定次数，请联系店主。", "This management link has no remaining CLI bindings. Contact the shop owner.");
      if (/已拒绝|denied/i.test(message)) return tr("此请求已拒绝，请让 CLI 重新申请。", "This request was denied. Create a new request in the CLI.");
      if (/已处理|已批准|already|consum/i.test(message)) return tr("此请求已处理，请查看 CLI 的登录结果。", "This request was already handled. Check the CLI sign-in result.");
      if (/认证|登录|范围|身份|会话|scope|session|reauth|sign.in|authenticated/i.test(message)) return tr("登录身份或授权范围已改变，请重新登录后核对设备。", "Your sign-in identity or authorization scope changed. Sign in again and review the device.");
      if (/摘要|权限.*变|review|changed/i.test(message)) return tr("授权信息已改变，请重新核对权限。", "Authorization details changed. Review the permissions again.");
      return tr("操作结果尚未确认，请查看 CLI；如未完成，可重新核对或申请设备码。", "The result is not confirmed. Check the CLI; if needed, review again or request a new device code.");
    };
    const run = async (handler) => {
      if (!active() || busy || state === "confirm") return;
      busy = true;
      const version = revision;
      controls(true); setError("");
      try { await handler(); }
      catch (error) { if (active() && version === revision) setError(errorMessage(error)); }
      finally { busy = false; if (active() && state !== "confirm") controls(false); }
    };
    async function checkScope() {
      const current = await request("/auth/status");
      if (!active()) return false;
      if (!signedIn(current) || authority(current) !== authority(auth) || current.session_id !== auth.session_id) throw new Error("Browser session scope changed");
      return true;
    }
    function validateScope(scope) {
      if (!scope || !uuid.test(scope.staff_id || "") || !uuid.test(scope.product_id || "") || !uuid.test(scope.shop_id || "")
        || [scope.link_name, scope.product_name, scope.shop_name].some((name) => typeof name !== "string" || !name.trim() || name.length > 1000)
        || !Array.isArray(scope.permissions) || !scope.permissions.length || scope.permissions.length > 32 || new Set(scope.permissions).size !== scope.permissions.length
        || scope.permissions.some((permission) => typeof permission !== "string" || !/^[a-z][a-z0-9_.-]{1,64}$/.test(permission))
        || !Number.isFinite(scope.expires) || scope.expires * 1000 <= Date.now()
        || !Number.isSafeInteger(scope.remaining_cli_uses) || scope.remaining_cli_uses < 0 || (scope.remaining_cli_uses === 0 && scope.already_bound !== true)
        || (scope.already_bound !== undefined && typeof scope.already_bound !== "boolean")
        || (staff(auth) && (scope.staff_id !== auth.link_id || scope.product_id !== auth.product_id || scope.shop_id !== auth.shop_id))
        || (owner(auth) && auth.superadmin !== true && scope.shop_id !== auth.shop_id)) throw new Error("Authorization scope changed or invalid");
    }
    function validate(value, selectedId = null) {
      const details = value?.request;
      if (!details || !uuid.test(details.request_id || "") || normalizeCode(details.user_code) !== normalizeCode(code)
        || !alphabet.test(normalizeCode(details.user_code)) || typeof details.client_name !== "string" || !details.client_name.trim() || details.client_name.length > 200 || !validAgentType(details.agent_type)
        || !digest.test(details.fingerprint || "") || !Number.isFinite(details.expires) || details.expires * 1000 <= Date.now()
        || !(details.product_id === null || uuid.test(details.product_id || "")) || !Array.isArray(value.candidates) || value.candidates.length > 1000) throw new Error("Device authorization review invalid or expired");
      const ids = new Set();
      for (const scope of value.candidates) { validateScope(scope); if (ids.has(scope.staff_id)) throw new Error("Authorization scope changed or invalid"); ids.add(scope.staff_id); }
      if (selectedId) {
        validateScope(value.selected);
        if (!digest.test(value.review_digest || "") || !digest.test(value.snapshot_digest || "") || value.selected.staff_id !== selectedId || !ids.has(selectedId)
          || (details.product_id !== null && details.product_id !== value.selected.product_id)
          || scopeSignature(value.selected) !== scopeSignature(value.candidates.find((scope) => scope.staff_id === selectedId))) throw new Error("Authorization scope changed or invalid");
      } else if (value.selected != null || value.review_digest != null) throw new Error("Authorization review invalid");
      return value;
    }
    const readOptions = async (selection = null) => {
      const body = typeof selection === "string" ? { staff_id: selection } : selection || {};
      const result = await request("/manage/device/options", { user_code: code, ...body });
      if (!active()) return null;
      if (result?.flow === "scope") {
        if (typeof selection === "string") throw new Error("Authorization scope changed");
        return validateActive(result, selection);
      }
      if (result?.flow !== undefined && result.flow !== "link") throw new Error("Unknown authorization scope");
      if (selection && typeof selection !== "string") throw new Error("Authorization scope changed or invalid");
      return validate(result, selection);
    };
    const scopeSignature = (scope) => JSON.stringify([scope.staff_id, scope.link_name, scope.product_id, scope.product_name, scope.shop_id, scope.shop_name, scope.permissions, scope.expires, scope.remaining_cli_uses, scope.already_bound === true]);
    const signature = (value) => JSON.stringify([value.request.request_id, normalizeCode(value.request.user_code), value.request.client_name, value.request.agent_type ?? null, value.request.fingerprint, value.request.product_id, value.request.expires, scopeSignature(value.selected), value.snapshot_digest]);
    const changed = (previous, next, allowSessionDigestRefresh = false) => signature(previous) !== signature(next) || (!allowSessionDigestRefresh && previous.review_digest !== next.review_digest);
    const changedMessage = () => tr("授权信息已改变。请重新核对下面的设备与权限，再决定是否授权。", "Authorization details changed. Review the device and permissions below before deciding again.");
    const queuePermissions = ["queue.view", "queue.process", "queue.retry", "queue.monitor"];
    const uniqueStrings = (values, valid, minimum = 1, maximum = 500) => Array.isArray(values) && values.length >= minimum && values.length <= maximum && new Set(values).size === values.length && values.every((value) => typeof value === "string" && valid(value));
    const subset = (values, allowed) => values.every((value) => allowed.includes(value));
    const sameSet = (left, right) => left.length === right.length && subset(left, right);
    const activePermissionLabel = (permission) => permission === "links.delegate"
      ? tr("生成更小权限的管理链接（每链接最多 1 次浏览器 / CLI 绑定）", "Generate links with fewer permissions (at most 1 browser / CLI binding per link)")
      : tr(...permissions[permission]);
    function validPermissions(values, kind) {
      const allowed = kind === "shop.pipeline" ? queuePermissions : Object.keys(permissions);
      return uniqueStrings(values, (value) => allowed.includes(value), 1, allowed.length)
        && (!values.some((value) => ["queue.process", "queue.retry"].includes(value)) || values.includes("queue.view"))
        && (!values.includes("fulfillment.configure") || values.includes("product.edit"));
    }
    function validateActive(value, selection = null) {
      const details = value?.request, current = value?.current, selected = value?.selected, shop = value?.shop;
      if (!owner(auth)) throw new Error("Shop owner authorization scope required");
      if (!details || !uuid.test(details.request_id || "") || normalizeCode(details.user_code) !== normalizeCode(code)
        || !alphabet.test(normalizeCode(details.user_code)) || typeof details.client_name !== "string" || !details.client_name.trim() || details.client_name.length > 200 || !validAgentType(details.agent_type)
        || !digest.test(details.fingerprint || "") || !Number.isFinite(details.expires) || details.expires * 1000 <= Date.now()
        || !["product", "shop.pipeline"].includes(details.kind) || !uuid.test(details.shop_id || "")
        || !uniqueStrings(details.requested_product_ids, (id) => uuid.test(id)) || (details.kind === "product" && details.requested_product_ids.length !== 1)
        || !validPermissions(details.requested_permissions, details.kind) || !Number.isFinite(details.grant_expires) || details.grant_expires * 1000 <= Date.now()
        || typeof details.reason !== "string" || details.reason.length > 1000
        || !(details.authorization_id === null || uuid.test(details.authorization_id || ""))
        || !(details.expected_revision === null || (Number.isSafeInteger(details.expected_revision) && details.expected_revision >= 1))
        || ((details.authorization_id === null) !== (details.expected_revision === null))
        || !shop || shop.id !== details.shop_id || typeof shop.name !== "string" || !shop.name.trim() || shop.name.length > 1000
        || (auth.superadmin !== true && auth.shop_id !== details.shop_id)
        || !Array.isArray(value.products) || value.products.length !== details.requested_product_ids.length
        || !digest.test(value.review_digest || "") || !digest.test(value.snapshot_digest || "")) throw new Error("Authorization scope changed or invalid");
      const productIds = [];
      for (const product of value.products) {
        if (!product || !uuid.test(product.id || "") || productIds.includes(product.id) || !details.requested_product_ids.includes(product.id)
          || typeof product.name !== "string" || !product.name.trim() || product.name.length > 1000
          || !["manual", "webhook", "script"].includes(product.mode) || (details.kind === "shop.pipeline" && product.mode !== "manual")) throw new Error("Authorization product scope changed or invalid");
        productIds.push(product.id);
      }
      if (current !== null) {
        if (!current || !uuid.test(current.id || "") || current.shop_id !== details.shop_id || current.kind !== details.kind
          || !uniqueStrings(current.product_ids, (id) => uuid.test(id)) || !subset(current.product_ids, details.requested_product_ids)
          || !validPermissions(current.permissions, details.kind) || !subset(current.permissions, details.requested_permissions)
          || !Number.isFinite(current.expires) || current.expires * 1000 <= Date.now()
          || !Number.isSafeInteger(current.revision) || current.revision < 1 || current.fingerprint !== details.fingerprint
          || typeof current.client_name !== "string" || !current.client_name.trim() || current.client_name.length > 200 || !validAgentType(current.agent_type)
          || (details.authorization_id !== null && (current.id !== details.authorization_id || current.revision !== details.expected_revision))) throw new Error("Current authorization scope changed or invalid");
      } else if (details.authorization_id !== null) throw new Error("Current authorization scope changed or invalid");
      if (!selected || selected.shop_id !== details.shop_id || selected.kind !== details.kind
        || !uniqueStrings(selected.product_ids, (id) => uuid.test(id)) || !subset(selected.product_ids, details.requested_product_ids)
        || !validPermissions(selected.permissions, details.kind) || !subset(selected.permissions, details.requested_permissions)
        || !Number.isFinite(selected.expires) || selected.expires * 1000 <= Date.now() || selected.expires > details.grant_expires
        || (current && (!subset(current.product_ids, selected.product_ids) || !subset(current.permissions, selected.permissions) || selected.expires !== current.expires))) throw new Error("Selected authorization scope changed or invalid");
      if (selection && (!sameSet(selection.product_ids || [], selected.product_ids) || !sameSet(selection.permissions || [], selected.permissions) || selection.expires !== selected.expires)) throw new Error("Selected authorization scope changed or invalid");
      return value;
    }
    const activeDraft = (value) => ({ product_ids: [...value.selected.product_ids], permissions: [...value.selected.permissions], expires: value.selected.expires });
    const activeSignature = (value) => {
      const details = value.request, current = value.current, selected = value.selected;
      return JSON.stringify([
        details.request_id, normalizeCode(details.user_code), details.client_name, details.agent_type ?? null, details.fingerprint, details.expires, details.kind, details.shop_id,
        [...details.requested_product_ids].sort(), [...details.requested_permissions].sort(), details.grant_expires, details.authorization_id, details.expected_revision, details.reason,
        value.shop.id, value.shop.name, [...value.products].sort((a, b) => a.id.localeCompare(b.id)).map((product) => [product.id, product.name, product.mode]),
        current && [current.id, current.shop_id, current.kind, [...current.product_ids].sort(), [...current.permissions].sort(), current.expires, current.revision, current.client_name, current.agent_type ?? null, current.fingerprint],
        selected.shop_id, selected.kind, [...selected.product_ids].sort(), [...selected.permissions].sort(), selected.expires, value.snapshot_digest,
      ]);
    };
    const activeChanged = (previous, next, allowSessionDigestRefresh = false) => activeSignature(previous) !== activeSignature(next) || (!allowSessionDigestRefresh && previous.review_digest !== next.review_digest);
    const localTime = (seconds) => { const value = new Date(seconds * 1000); return new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
    const checkedValues = (name) => [...root.querySelectorAll(`input[name="${name}"]`)].filter((node) => node.checked).map((node) => node.value);
    const deviceMetadata = (details, shop) => `${window.ExtoreWorkerIdentity?.markup({ name: details.client_name, kind: "cli", agent_type: details.agent_type ?? null }, { language: tr("zh-CN", "en") }) || ""}<p class="caption">${tr("名称和类型由发起者声明，未经验证；请核对自己 CLI 上的设备码与指纹。", "Names and types are declared by the requester and are unverified. Compare the device code and fingerprint with your own CLI.")}</p><dl class="device-details"><dt>${tr("设备码", "Device code")}</dt><dd class="mono">${esc(displayCode(details.user_code))}</dd><dt>${tr("设备名称", "Device name")}</dt><dd>${esc(details.client_name)}</dd><dt>${tr("代理类型（自报）", "Agent type (self-reported)")}</dt><dd>${esc(details.agent_type || tr("未声明", "Not declared"))}</dd><dt>${tr("设备指纹", "Device fingerprint")}</dt><dd class="mono device-fingerprint">${esc(details.fingerprint)}</dd><dt>${tr("店铺", "Shop")}</dt><dd>${esc(shop.name)}</dd><dt>${tr("此次请求有效期", "Request expires")}</dt><dd>${esc(date(details.expires))}</dd></dl>`;
    function activeSelectionPage(value, message = "", preserveSelection = false) {
      state = "scope"; review = null;
      const details = value.request, current = value.current, retainedProducts = current?.product_ids || [], retainedPermissions = current?.permissions || [];
      const selectedProducts = current && !preserveSelection ? retainedProducts : value.selected.product_ids;
      const selectedPermissions = current && !preserveSelection ? retainedPermissions : preserveSelection ? value.selected.permissions : value.selected.permissions.filter((permission) => permission !== "links.delegate");
      const expires = current?.expires || value.selected.expires, inputTime = localTime(expires);
      const title = current ? (details.authorization_id ? tr("申请追加权限", "Requested additional permissions") : tr("恢复或追加设备授权", "Restore or extend device authorization")) : tr("选择本次授权", "Choose this authorization");
      page(`<section class="panel"><h2>${title}</h2>${deviceMetadata(details, value.shop)}${details.reason ? `<p class="device-request-reason"><strong>${tr("CLI 申请说明", "CLI request reason")}</strong><br>${esc(details.reason)}</p>` : ""}<p class="caption">${tr("只授权下面列出的当前商品。以后新增的商品，需要 CLI 再次申请并由你批准。", "Only the current products listed below can be authorized. Products added later require another CLI request and your approval.")}</p><form id="device-active-selection"><fieldset class="device-selection-fields"><legend>${details.kind === "shop.pipeline" ? tr("本店当前队列商品", "Current shop queue products") : tr("指定商品", "Requested product")} · ${value.products.length}</legend><div class="device-product-choices">${value.products.map((product) => `<label class="device-checkbox" for="device-product-${esc(product.id)}"><input id="device-product-${esc(product.id)}" type="checkbox" name="device-product" value="${esc(product.id)}" data-device-selection data-device-fixed="${retainedProducts.includes(product.id)}" ${selectedProducts.includes(product.id) ? "checked" : ""} ${retainedProducts.includes(product.id) ? "disabled" : ""}><span>${esc(product.name)}<span class="caption mono">${esc(product.id)}</span>${current ? `<span class="caption">${retainedProducts.includes(product.id) ? tr("已授权，保持现有范围", "Already authorized; retained") : tr("申请新增商品，需单独选择", "Requested new product; select explicitly")}</span>` : ""}</span></label>`).join("")}</div>${value.products.length > 1 ? `<button id="device-select-all-products" type="button" class="secondary">${tr("选择上述全部当前商品", "Select all current products above")}</button>` : ""}</fieldset><fieldset class="device-selection-fields form-divider"><legend>${tr("本次授予的商品权限", "Product permissions for this authorization")}</legend><p class="caption">${details.kind === "shop.pipeline" ? tr("全店队列授权仅允许查看、处理和重试任务。", "Shop queue authorization allows only viewing, processing and retrying tasks.") : tr("只授予这个商品的所选权限，不授予店主管理或平台管理权限。", "Grant only selected permissions for this product, without shop owner or platform administrator access.")}</p>${details.requested_permissions.map((permission) => `<label class="device-checkbox" for="device-permission-${esc(permission.replaceAll(".", "-"))}"><input id="device-permission-${esc(permission.replaceAll(".", "-"))}" type="checkbox" name="device-permission" value="${esc(permission)}" data-device-selection data-device-fixed="${retainedPermissions.includes(permission)}" ${selectedPermissions.includes(permission) ? "checked" : ""} ${retainedPermissions.includes(permission) ? "disabled" : ""}><span>${esc(activePermissionLabel(permission))}<span class="caption mono">${esc(permission)}</span>${current ? `<span class="caption">${retainedPermissions.includes(permission) ? tr("已有权限，保持不变", "Existing permission; retained") : tr("申请新增权限，需单独选择", "Requested new permission; select explicitly")}</span>` : ""}</span></label>`).join("")}</fieldset><div class="field form-divider"><label for="device-grant-expires">${tr("授权到期时间（本机时区）", "Authorization expires (local time zone)")}</label><input id="device-grant-expires" type="datetime-local" value="${esc(inputTime)}" max="${esc(localTime(details.grant_expires))}" data-device-selection data-device-fixed="${!!current}" ${current ? "disabled" : "required"}><p class="caption">${current ? tr("追加或恢复保持现有授权的到期时间，不延长也不撤销原权限。", "Extending or restoring preserves the existing expiration, without extending or revoking existing access.") : tr("可以缩短授权时间，不能超过 CLI 本次申请的期限。", "You may shorten this authorization, but cannot exceed the expiration requested by the CLI.")}</p></div><button id="device-select-review" type="submit" class="full">${tr("核对所选授权与新增权限", "Review selected authorization and additions")}</button></form><div class="actions device-actions form-divider"><button id="device-deny" type="button" class="danger">${current ? tr("拒绝本次申请，保留原授权", "Deny this request; retain existing access") : tr("拒绝本次申请", "Deny this request")}</button><button id="device-back" type="button" class="secondary">${tr("返回设备码", "Back to device code")}</button></div></section>`, message);
      bind("#device-select-all-products", "click", () => { if (!busy) root.querySelectorAll('input[name="device-product"]').forEach((node) => { if (!node.disabled) node.checked = true; }); });
      bind("#device-back", "click", () => { if (!busy) codePage(); });
      bind("#device-deny", "click", () => run(() => denyActive(value)));
      bind("#device-active-selection", "submit", () => run(async () => {
        const draft = { product_ids: checkedValues("device-product"), permissions: checkedValues("device-permission"), expires: current ? current.expires : $("#device-grant-expires")?.value === inputTime ? expires : new Date($("#device-grant-expires")?.value || "").getTime() / 1000 };
        if (!draft.product_ids.length || !draft.permissions.length) { setError(tr("请至少选择一个商品和一项权限。", "Select at least one product and one permission.")); return; }
        if (!validPermissions(draft.permissions, details.kind)) { setError(tr("处理或重试需同时选择查看队列；配置发货需同时选择修改商品。", "Processing or retrying requires queue viewing; configuring fulfillment requires product editing.")); return; }
        if (!Number.isFinite(draft.expires) || draft.expires * 1000 <= Date.now() || draft.expires > details.grant_expires) { setError(tr("请选择有效的到期时间，且不能晚于本次申请期限。", "Choose a future expiration no later than the requested limit.")); return; }
        if (!await checkScope()) return;
        const latest = await readOptions(draft);
        if (!latest || latest.flow !== "scope") throw new Error("Authorization scope changed");
        activeReviewPage(latest);
      }));
    }
    const permissionList = (values) => values.length ? `<ul class="device-permissions">${values.map((permission) => `<li>${esc(activePermissionLabel(permission))} <span class="caption mono">${esc(permission)}</span></li>`).join("")}</ul>` : `<p class="caption">${tr("无", "None")}</p>`;
    function activeReviewPage(value, message = "") {
      state = "review"; review = value;
      const details = value.request, current = value.current, selected = value.selected;
      const newlySelectedProducts = selected.product_ids.filter((id) => !current?.product_ids.includes(id));
      const newlySelectedPermissions = selected.permissions.filter((permission) => !current?.permissions.includes(permission));
      const restoringOnly = !!current && !newlySelectedProducts.length && !newlySelectedPermissions.length;
      page(`<section class="panel"><h2>${current ? tr("核对现有与本次新增授权", "Review existing access and requested additions") : tr("核对后批准本次申请", "Review before approving this request")}</h2>${deviceMetadata(details, value.shop)}<p class="caption">${tr("只包含下列具体商品；之后的新商品不会自动获得权限。", "Only the specific products below are included; future products receive no automatic access.")}</p>${current ? `<div class="form-divider"><h3>${tr("现有授权，保持不变", "Existing authorization, retained")}</h3><p class="caption">${tr("授权版本", "Authorization revision")} ${esc(current.revision)} · ${esc(date(current.expires))}</p><p>${tr("已有设备名称", "Existing device name")}：${esc(current.client_name)} · ${esc(current.agent_type || tr("未声明类型", "Type not declared"))}</p><ul class="device-product-summary">${current.product_ids.map((id) => `<li>${esc(value.products.find((product) => product.id === id)?.name || id)} <span class="caption mono">${esc(id)}</span></li>`).join("")}</ul>${permissionList(current.permissions)}</div>` : ""}<div class="form-divider"><h3>${tr("本次批准的商品", "Products approved in this request")} · ${selected.product_ids.length}</h3><ul class="device-product-summary">${selected.product_ids.map((id) => `<li>${esc(value.products.find((product) => product.id === id)?.name || id)}${current ? `<span class="device-change-label">${newlySelectedProducts.includes(id) ? tr("新增商品", "New product") : tr("已有商品", "Existing product")}</span>` : ""}<span class="caption mono">${esc(id)}</span></li>`).join("")}</ul><h3>${tr("将授予的全部商品权限", "All product permissions to be granted")}</h3>${permissionList(selected.permissions)}<p class="caption">${tr("授权到期", "Authorization expires")}：${esc(date(selected.expires))}</p></div><details class="device-requested-scope form-divider"><summary>${tr("核对 CLI 原始申请与未批准范围", "Review the original CLI request and unapproved scope")}</summary><h3>${tr("申请的商品", "Requested products")}</h3><ul class="device-product-summary">${value.products.map((product) => `<li>${esc(product.name)} <span class="caption">${selected.product_ids.includes(product.id) ? tr("本次包含", "Included") : tr("本次未批准", "Not approved")}</span></li>`).join("")}</ul><h3>${tr("申请的权限", "Requested permissions")}</h3>${permissionList(details.requested_permissions)}<h3>${tr("本次未批准的权限", "Permissions not approved")}</h3>${permissionList(details.requested_permissions.filter((permission) => !selected.permissions.includes(permission)))}</details>${current ? `<div class="form-divider"><h3>${tr("本次新增权限", "Additional permissions in this request")}</h3>${permissionList(newlySelectedPermissions)}<p class="caption">${tr(`新增 ${newlySelectedProducts.length} 个商品。已有商品也只会增加上面明确列出的权限；拒绝本次申请不影响原授权。`, `${newlySelectedProducts.length} additional products. Existing products receive only the additional permissions explicitly listed above. Denying this request does not affect existing access.`)}</p></div>` : ""}<p class="caption">${restoringOnly ? tr("本次仅恢复当前授权，没有新增商品或权限，也不会延长到期时间。", "This request only restores existing access, with no additional products, permissions or extended expiration.") : ""} ${tr("批准后仍需 CLI 使用自己的设备密钥领取，届时本次授权或新增权限才生效。", "After approval, the CLI must claim with its own device key before this authorization or additional permissions take effect.")}</p><div class="actions device-actions"><button id="device-approve" type="button">${restoringOnly ? tr("恢复当前授权", "Restore existing access") : current ? tr("批准所选追加权限", "Approve selected additional access") : tr("批准所选商品权限", "Approve selected product permissions")}</button><button id="device-deny" type="button" class="danger">${current ? tr("拒绝本次申请，保留原授权", "Deny request; retain existing access") : tr("拒绝本次申请", "Deny this request")}</button><button id="device-back" type="button" class="secondary">${tr("调整所选范围", "Adjust selection")}</button></div><div id="device-confirmation"></div></section>`, message);
      bind("#device-back", "click", () => { if (!busy) activeSelectionPage(value, "", true); });
      bind("#device-deny", "click", () => run(() => denyActive(value)));
      bind("#device-approve", "click", () => run(async () => {
        const previous = review;
        if (!await checkScope()) return;
        const latest = await readOptions(activeDraft(previous));
        if (!latest || latest.flow !== "scope") throw new Error("Authorization scope changed");
        if (activeChanged(previous, latest)) { activeReviewPage(latest, changedMessage()); return; }
        confirmOwner(latest, approveActive, activeReviewPage);
      }));
    }
    async function approveActive(previous, allowSessionDigestRefresh = false) {
      if (!active()) return;
      const version = revision;
      if (!await checkScope()) return;
      const latest = await readOptions(activeDraft(previous));
      if (!latest || !active() || revision !== version) return;
      if (latest.flow !== "scope") throw new Error("Authorization scope changed");
      if (activeChanged(previous, latest, allowSessionDigestRefresh)) { activeReviewPage(latest, changedMessage()); return; }
      const result = await request("/manage/device/approve", { user_code: code, ...activeDraft(latest), review_digest: latest.review_digest });
      if (!active() || revision !== version) return;
      if (result?.ok !== true || result.status !== "approved") throw new Error("Device result not confirmed");
      finishedActive(true, !!latest.current);
    }
    async function denyActive(value) {
      if (!await checkScope()) return;
      const version = revision;
      const result = await request("/manage/device/deny", { user_code: code });
      if (!active() || revision !== version) return;
      if (result?.ok !== true || result.status !== "denied") throw new Error("Device result not confirmed");
      finishedActive(false, !!value.current);
    }
    function finishedActive(approved, hadCurrent) {
      state = approved ? "approved" : "denied"; review = null; code = "";
      page(`<section class="panel"><h2>${approved ? tr("已批准，等待 CLI 领取", "Approved; waiting for CLI claim") : tr("已拒绝本次申请", "This request was denied")}</h2><p role="status">${approved ? tr("回到 CLI 等待领取。本次授权或追加权限将在 CLI 确认设备密钥并领取后生效；页面不会显示访问密钥。", "Return to the CLI and wait for claim. This authorization or additional access takes effect only after the CLI confirms its device key and claims it. This page does not display access credentials.") : hadCurrent ? tr("原有授权与正在处理的任务保持不变，仅拒绝本次申请。", "Existing access and tasks in progress remain unchanged. Only this request was denied.") : tr("此请求已拒绝，未创建商品授权。", "This request was denied without creating product access.")}</p><button id="device-another" type="button" class="secondary">${tr("输入另一个设备码", "Enter another device code")}</button></section>`);
      bind("#device-another", "click", () => { if (!busy) codePage(); });
    }
    function codePage(message = "") {
      state = "code"; review = null;
      page(`<p>${tr("只输入你本人或你刚要求 AI 发起的 CLI 操作所显示的设备码。不要替陌生人授权；请核对设备指纹和权限。", "Enter only a code from your own CLI or a CLI operation you just asked your AI to start. Do not authorize a stranger's device. Check its fingerprint and permissions.")}</p><section class="panel"><form id="device-code-form"><div class="field"><label for="device-code">${tr("CLI 设备码", "CLI device code")}</label><input id="device-code" type="text" class="code" required autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="14" placeholder="XXXX-XXXX-XXXX" value="${esc(code)}" aria-describedby="device-code-help"><p id="device-code-help" class="caption">${tr("设备码不是管理链接。核对设备后，还需你单独点击授权。", "A device code is not a management link. You must explicitly approve after reviewing the device.")}</p></div><button id="device-review" class="full" type="submit">${signedIn(auth) ? tr("核对设备与权限", "Review device and permissions") : tr("登录并核对设备", "Sign in and review device")}</button></form>${signedIn(auth) ? `<p class="caption">${owner(auth) ? tr(auth.superadmin === true ? "当前身份：平台管理员。" : "当前身份：店铺管理者。", auth.superadmin === true ? "Signed in as platform administrator." : "Signed in as shop owner.") : tr("当前身份：商品管理链接。只能授权本链接范围。", "Signed in with a product management link. Only this link's scope can be authorized.")}</p>` : `<div class="form-divider"><p class="caption">${tr("商品管理者可先在同一浏览器打开自己的管理链接建立登录，再回到此页；无需把链接交给 CLI。", "Product managers can open their management link in this browser, then return here. Do not send the link to the CLI.")}</p><button id="device-refresh" type="button" class="secondary full">${tr("我已登录，重新检查", "I signed in — check again")}</button></div>`}</section>`, message);
      bind("#device-code-form", "submit", () => run(async () => {
        const input = $("#device-code"), value = normalizeCode(input?.value);
        if (!alphabet.test(value)) { setError(tr("请输入 CLI 中完整的 12 位设备码。", "Enter the complete 12-character code from the CLI.")); input?.focus(); return; }
        code = displayCode(value);
        if (!signedIn(auth)) { loginPage(); return; }
        if (!await checkScope()) return;
        const options = await readOptions();
        if (options) options.flow === "scope" ? activeSelectionPage(options) : scopePage(options);
      }));
      bind("#device-refresh", "click", () => run(async () => {
        code = $("#device-code")?.value || code;
        const current = await request("/auth/status");
        if (!active()) return;
        auth = current; onAuth(current);
        codePage(signedIn(current) ? "" : tr("此浏览器尚未登录，请登录店铺或先打开自己的管理链接。", "This browser is not signed in. Sign in to your shop or open your management link first."));
      }));
    }
    function loginPage() {
      state = "login";
      page(`<p>${tr("先登录你的店铺账号。设备码只保留在当前页面，不会自动授权。", "Sign in to your shop account first. The device code stays only on this page; sign-in does not automatically authorize it.")}</p><div id="device-account"></div><button id="device-login-back" type="button" class="secondary">${tr("返回设备码", "Back to device code")}</button>`);
      if (!window.ExtoreAccount?.mount) { codePage(tr("登录模块暂不可用，请刷新页面。", "The sign-in module is unavailable. Refresh the page.")); return; }
      const version = revision;
      account = window.ExtoreAccount.mount({ root: $("#device-account"), api, auth, mode: "login", passkey, language, isCurrent: () => active() && revision === version,
        onAuth: (current) => { if (active() && revision === version) { auth = current; onAuth(current); } },
        navigate: async () => {
          try {
            const current = await request("/auth/status");
            if (!active() || revision !== version) return;
            auth = current; onAuth(current);
            codePage(signedIn(current) ? "" : tr("登录未完成，请重新登录。", "Sign-in did not complete. Try again."));
          } catch (error) { if (active() && revision === version) setError(errorMessage(error)); }
        },
      });
      bind("#device-login-back", "click", () => { if (active()) codePage(); });
    }
    function scopePage(options) {
      state = "scope"; review = null;
      const choices = options.candidates;
      page(`<section class="panel"><h2>${tr("选择授权范围", "Choose authorization scope")}</h2><p>${tr("以下是当前登录身份可授权、且仍有 CLI 绑定次数的商品管理链接。不会创建额外权限。", "These existing product links are available to your signed-in account and have CLI bindings remaining. No additional permissions are created.")}</p>${options.candidates_truncated === true ? `<p class="caption">${tr("链接较多，仅显示前 200 条。可让 CLI 使用 --product 指定商品后重新申请。", "Only the first 200 links are shown. Request a new code with --product in the CLI to choose a specific product.")}</p>` : ""}${choices.length ? `<form id="device-scope-form"><div class="field"><label for="device-scope">${tr("商品管理链接", "Product management link")}</label><select id="device-scope" required><option value="">${tr("请选择", "Choose a link")}</option>${choices.map((scope) => `<option value="${esc(scope.staff_id)}">${esc(scope.shop_name)} · ${esc(scope.product_name)} · ${esc(scope.link_name)}</option>`).join("")}</select></div><button id="device-select-review" type="submit" class="full">${tr("核对所选权限", "Review selected permissions")}</button></form>` : `<p>${tr("没有可用的管理链接。请店主创建所需商品的管理链接，或增加 CLI 绑定次数后重试。", "No management link is available. Ask the shop owner to create a product link or provide a CLI binding, then retry.")}</p>`}<button id="device-back" type="button" class="secondary full form-divider">${tr("返回设备码", "Back to device code")}</button></section>`);
      if (choices.length === 1) $("#device-scope").value = choices[0].staff_id;
      bind("#device-back", "click", () => { if (!busy) codePage(); });
      bind("#device-scope-form", "submit", () => run(async () => {
        const selected = $("#device-scope")?.value;
        if (!choices.some((scope) => scope.staff_id === selected)) { setError(tr("请选择一个管理链接。", "Choose a management link.")); return; }
        if (!await checkScope()) return;
        const value = await readOptions(selected);
        if (value) reviewPage(value);
      }));
    }
    function reviewPage(value, message = "") {
      state = "review"; review = value;
      const details = value.request, scope = value.selected;
      page(`<section class="panel"><h2>${tr("核对后再授权", "Review before approving")}</h2><p>${tr("名称和类型由发起者声明，未经验证。核对设备名称和指纹是否与你自己的 CLI 一致。授权只适用于下列商品和权限。", "Check that the device name and fingerprint match your CLI. Authorization applies only to the product and permissions listed below.")}</p><dl class="device-details"><dt>${tr("设备码", "Device code")}</dt><dd class="mono">${esc(displayCode(details.user_code))}</dd><dt>${tr("设备名称", "Device name")}</dt><dd>${esc(details.client_name)}</dd><dt>${tr("代理类型（自报）", "Agent type (self-reported)")}</dt><dd>${esc(details.agent_type || tr("未声明", "Not declared"))}</dd><dt>${tr("设备指纹", "Device fingerprint")}</dt><dd class="mono device-fingerprint">${esc(details.fingerprint)}</dd><dt>${tr("店铺", "Shop")}</dt><dd>${esc(scope.shop_name)}</dd><dt>${tr("商品", "Product")}</dt><dd>${esc(scope.product_name)}</dd><dt>${tr("管理链接", "Management link")}</dt><dd>${esc(scope.link_name)}</dd><dt>${tr("权限有效期", "Grant expires")}</dt><dd>${esc(date(scope.expires))}</dd><dt>${tr("此次请求有效期", "Request expires")}</dt><dd>${esc(date(details.expires))}</dd></dl><div class="form-divider"><h3>${tr("将授予的全部权限", "All permissions to be granted")}</h3><ul class="device-permissions">${scope.permissions.map((permission) => `<li>${esc(permissions[permission] ? tr(...permissions[permission]) : permission)} <span class="caption mono">${esc(permission)}</span></li>`).join("")}</ul></div><p class="caption">${scope.already_bound === true ? tr("恢复已绑定的 CLI 设备，不新增绑定次数。浏览器登录次数不受影响。", "Restore this already-bound CLI device without using another binding. Browser bindings are unaffected.") : tr(`批准将使用 1 次 CLI 绑定机会，当前剩余 ${scope.remaining_cli_uses} 次。浏览器登录次数不受影响。`, `Approval uses 1 CLI binding. ${scope.remaining_cli_uses} remain. Browser bindings are unaffected.`)}</p><div class="actions device-actions"><button id="device-approve" type="button">${tr("授权这个设备", "Authorize this device")}</button><button id="device-deny" type="button" class="danger">${tr("拒绝授权", "Deny authorization")}</button><button id="device-back" type="button" class="secondary">${tr("重新选择", "Choose again")}</button></div><div id="device-confirmation"></div></section>`, message);
      bind("#device-back", "click", () => { if (!busy) codePage(); });
      bind("#device-deny", "click", () => run(async () => {
        if (!await checkScope()) return;
        const result = await request("/manage/device/deny", { user_code: code });
        if (!active()) return;
        if (result?.ok !== true || result.status !== "denied") throw new Error("Device result not confirmed");
        finished(false);
      }));
      bind("#device-approve", "click", () => run(async () => {
        const previous = review;
        if (!await checkScope()) return;
        const latest = await readOptions(previous.selected.staff_id);
        if (!latest) return;
        if (changed(previous, latest)) { reviewPage(latest, changedMessage()); return; }
        if (owner(auth)) confirmOwner(latest);
        else await approve(latest);
      }));
    }
    async function approve(previous, allowSessionDigestRefresh = false) {
      if (!active()) return;
      const version = revision;
      if (!await checkScope()) return;
      const latest = await readOptions(previous.selected.staff_id);
      if (!latest || !active() || revision !== version) return;
      if (changed(previous, latest, allowSessionDigestRefresh)) { reviewPage(latest, changedMessage()); return; }
      const result = await request("/manage/device/approve", { user_code: code, staff_id: latest.selected.staff_id, review_digest: latest.review_digest });
      if (!active() || revision !== version) return;
      if (result?.ok !== true || result.status !== "approved") throw new Error("Device result not confirmed");
      finished(true);
    }
    function confirmOwner(value, approveOperation = approve, restoreReview = reviewPage) {
      if (!active() || !window.ExtoreAccount?.mount) throw new Error("Account verification unavailable");
      state = "confirm"; controls(true);
      const version = revision, previousAuth = auth;
      account = window.ExtoreAccount.mount({ root: $("#device-confirmation"), api, auth, mode: "confirm", passkey, language, isCurrent: () => active() && revision === version && state === "confirm",
        navigate,
        onAuth: (current) => {
          if (!active() || revision !== version) return;
          if (!signedIn(current) || !owner(current) || authority(previousAuth) !== authority(current)) throw new Error("Browser session scope changed");
          auth = current; onAuth(current);
        },
      });
      account.confirmFresh(async () => {
        deciding = true;
        const cancel = $("#device-confirmation")?.querySelector("#account-fresh-cancel");
        if (cancel) cancel.disabled = true;
        // Passkey reauthentication can rotate the session that the digest binds.
        // Only a verified rotation of the same account permits a new digest;
        // every displayed device and scope field must still match the review.
        try { await approveOperation(value, auth.session_id !== previousAuth.session_id); }
        catch (error) { if (active() && revision === version) restoreReview(value, errorMessage(error)); }
        finally { if (active() && revision === version) { deciding = false; if (cancel?.isConnected !== false) cancel.disabled = false; } }
      }, tr("确认授权 CLI 设备", "Confirm CLI device authorization"));
      $("#device-confirmation")?.querySelector("#account-fresh-cancel")?.addEventListener("click", () => {
        if (!deciding && active() && revision === version) restoreReview(value, tr("已取消本次授权。", "Authorization cancelled."));
      });
    }
    function finished(approved) {
      state = approved ? "approved" : "denied"; review = null; code = "";
      page(`<section class="panel"><h2>${approved ? tr("已授权", "Authorized") : tr("已拒绝", "Denied")}</h2><p role="status">${approved ? tr("回到 CLI，等待它领取登录结果。此页面不会显示访问密钥。", "Return to the CLI and wait for it to receive the sign-in result. This page does not display access credentials.") : tr("此设备请求已拒绝，未占用 CLI 绑定次数。", "This device request was denied without using a CLI binding.")}</p><button id="device-another" class="secondary" type="button">${tr("输入另一个设备码", "Enter another device code")}</button></section>`);
      bind("#device-another", "click", () => { if (!busy) codePage(); });
    }
    const leave = () => instance.dispose();
    const instance = Object.freeze({ dispose() { if (disposed) return; disposed = true; revision++; controller.abort(); stopAccount(); review = null; code = ""; window.removeEventListener?.("pagehide", leave); if (mounts.get(root) === instance) mounts.delete(root); }, get active() { return active(); }, get state() { return state; } });
    mounts.set(root, instance);
    window.addEventListener?.("pagehide", leave);
    try {
      const origin = new URL(window.location?.origin || "");
      if (origin.protocol !== "https:" && !(origin.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname))) throw new Error("Unsafe origin");
      codePage();
    } catch {
      state = "invalid";
      page(`<p class="error" role="alert">${tr("请通过本站 HTTPS 地址进行设备授权。", "Use this site's HTTPS address for device authorization.")}</p>`);
    }
    return instance;
  }
  window.ExtoreDeviceLogin = Object.freeze({ mount, normalizeCode });
})();
