"use strict";

(() => {
  const mounts = new WeakMap();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const requestIdentifier = /^[A-Za-z0-9_-]{43}$/;
  const digest = /^[a-f0-9]{64}$/;
  const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const productIdentifier = /^[A-Za-z0-9_-]{1,100}$/;
  const allowedScopes = new Set(["products.read", "cards.issue"]);
  const docs = "https://github.com/TokenNotIncluded/extore/blob/main/docs/commerce-import-protocol.md";
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const text = (value, limit = 1000) => typeof value === "string" && value.trim().length > 0 && value.length <= limit && !/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value);
  const unique = (values) => Array.isArray(values) && new Set(values).size === values.length;
  const owner = (value) => value?.role === "admin" && typeof value.session_id === "string" && value.session_id.length > 0 && value.channel !== "cli" && (value.superadmin === true ? value.shop_id === null : uuid.test(value.shop_id || ""));
  const identity = (value) => JSON.stringify([value?.role, value?.shop_id, value?.superadmin === true, value?.account_id || null]);
  const now = () => Math.floor(Date.now() / 1000);
  let sequence = 0;

  function callbackURL(value) {
    if (typeof value !== "string" || value.length > 2048 || /[\s\\]/u.test(value)) throw new Error("Invalid callback URL");
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.hash || !url.hostname || !url.hostname.includes(".") || /[^\x00-\x7f]/.test(value)
      || /^[\d.]+$/.test(url.hostname) || !/^[A-Za-z0-9.-]+$/.test(url.hostname) || url.hostname.endsWith(".") || /\.(?:localhost|local|internal|onion)$/i.test(url.hostname)
      || url.hostname.split(".").some((label) => !label || label.length > 63)) throw new Error("Invalid callback URL");
    return url;
  }
  function safeRedirect(result, registered, expected = "approved") {
    if (result?.ok !== true) throw new Error("Unconfirmed authorization result");
    const destination = callbackURL(result.redirect_uri), bound = callbackURL(registered);
    if (destination.origin !== bound.origin || destination.pathname !== bound.pathname) throw new Error("Authorization callback changed");
    const reserved = new Set(["code", "state", "iss", "error", "error_description"]);
    for (const key of reserved) if (bound.searchParams.has(key)) throw new Error("Unsafe registered callback");
    const names = new Set([...bound.searchParams.keys(), ...destination.searchParams.keys()]);
    for (const key of names) {
      const original = bound.searchParams.getAll(key), actual = destination.searchParams.getAll(key);
      if (reserved.has(key)) { if (actual.length > 1) throw new Error("Duplicate callback parameter"); }
      else if (JSON.stringify(original) !== JSON.stringify(actual)) throw new Error("Authorization callback query changed");
    }
    if (!text(destination.searchParams.get("iss"), 2048) || !text(destination.searchParams.get("state"), 1024)) throw new Error("Missing callback binding");
    if (expected === "approved") {
      if (!text(destination.searchParams.get("code"), 2048) || destination.searchParams.has("error") || destination.searchParams.has("error_description")) throw new Error("Invalid approval callback");
    } else if (destination.searchParams.get("error") !== "access_denied" || destination.searchParams.has("code")) throw new Error("Invalid denial callback");
    return destination.href;
  }
  function context(value, shopId, requestId) {
    const request = value?.request;
    if (!uuid.test(shopId || "") || !requestIdentifier.test(requestId || "") || !request || request.id !== requestId || !identifier.test(request.client_id || "")
      || !text(request.client_name, 100) || !text(request.redirect_host, 300) || !digest.test(value.review_digest || "")
      || !Number.isFinite(request.expires) || request.expires <= now() || !Number.isFinite(request.max_grant_expires) || request.max_grant_expires <= now()
      || !unique(request.scopes) || !request.scopes.length || request.scopes.some((scope) => !allowedScopes.has(scope))
      || !unique(request.requested_product_ids) || request.requested_product_ids.some((id) => !productIdentifier.test(id))
      || value.shop?.id !== shopId || !text(value.shop.name) || !Array.isArray(value.products) || value.products.length > 100) throw new Error("Invalid authorization context");
    const callback = callbackURL(request.redirect_uri);
    if (!/^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$/.test(request.redirect_host) || callbackURL("https://" + request.redirect_host).host !== callback.host) throw new Error("Callback host changed");
    for (const key of ["code", "state", "iss", "error", "error_description"]) if (callback.searchParams.has(key)) throw new Error("Unsafe registered callback");
    if (!unique(value.products.map((product) => product?.id))) throw new Error("Duplicate products");
    for (const product of value.products) {
      if (!productIdentifier.test(product.id || "") || !text(product.name) || !Array.isArray(product.variants) || product.variants.length > 100 || !unique(product.variants.map((variant) => variant?.id))) throw new Error("Invalid products");
      for (const variant of product.variants) if (!identifier.test(variant.id || "") || !text(variant.name) || ![true, false, 0, 1].includes(variant.enabled)) throw new Error("Invalid variants");
    }
    if (request.requested_product_ids.some((id) => !value.products.some((product) => product.id === id))) throw new Error("Requested products changed");
    if (JSON.stringify(value).length > 1000000) throw new Error("Authorization context too large");
    return structuredClone(value);
  }

  function mount({ root, api, auth = {}, mode = "management", requestId = "", language = "zh-CN", isCurrent = () => true, reauthenticate, navigate = (url) => { window.location.href = url; }, onAuth = () => {} } = {}) {
    if (!root?.querySelector || typeof api !== "function" || !["management", "authorize"].includes(mode)) throw new TypeError("Commerce connections require a root, API and valid mode");
    mounts.get(root)?.dispose();
    const prefix = "commerce-connect-" + ++sequence, controller = new AbortController();
    const readAuth = () => typeof auth === "function" ? auth() : auth;
    let currentAuth = readAuth(), initialIdentity = identity(currentAuth), sessionId = currentAuth?.session_id, disposed = false, generation = 0, busy = false, marker = null;
    let selectedShop = currentAuth?.superadmin === true ? "" : currentAuth?.shop_id || "", shops = [], clients = [], grants = [], showHistory = false, review = null, selection = null;
    const $ = (name) => root.querySelector("#" + prefix + "-" + name);
    const tr = (cn, en) => String(typeof language === "function" ? language() : language).toLowerCase().startsWith("en") ? en : cn;
    const date = (seconds) => Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString(tr("zh-CN", "en-GB")) : "—";
    const structural = () => { try { return !disposed && root.isConnected !== false && isCurrent() && (!marker || $("surface") === marker); } catch { return false; } };
    const current = () => structural() && identity(readAuth()) === initialIdentity && readAuth()?.session_id === sessionId;
    const alive = (version, shopId = selectedShop) => current() && generation === version && selectedShop === shopId;
    const request = (path, body, method = body === undefined ? "GET" : "POST") => api(path, body, method, { signal: controller.signal, expectedScope: currentAuth?.superadmin === true ? "platform" : currentAuth?.shop_id, expectedSessionId: sessionId });
    const scoped = (path, extras = {}) => path + "?" + new URLSearchParams({ shop_id: selectedShop, ...extras });
    const setError = (message) => { if (current() && $("error")) $("error").textContent = message; };
    const setStatus = (message) => { if (current() && $("status")) $("status").textContent = message; };
    const errorMessage = (error) => {
      const message = String(error?.message || "");
      if (error?.name === "NotAllowedError" || error?.name === "AbortError") return tr("身份验证已取消，尚未提交。你可以重试。", "Identity verification was cancelled. Nothing was submitted; you can retry.");
      if (/expired|过期|到期|已处理/i.test(message)) return tr("授权申请已到期或已处理。请回到商城重新发起连接。", "This authorization request expired or was already handled. Start a new connection in the marketplace.");
      if (error?.status === 429 || /too.many|频繁|频率/i.test(message)) return tr("操作太频繁，请稍后重试。", "Too many attempts. Try again shortly.");
      if (/应用.*上限|数量已达上限|client.*limit/i.test(message)) return tr("商城应用数量已达上限。先撤销不用的应用，再注册。", "The marketplace app limit was reached. Revoke an unused app before registering another.");
      if (error?.status === 401 || /401|reauth|fresh|认证|验证|session/i.test(message)) return tr("需要重新确认店主身份。请重新登录或验证后重试。", "Confirm the shop owner's identity again. Sign in or verify, then retry.");
      if (error?.status === 403 || /scope|范围|权限/i.test(message)) return tr("当前账号没有这个店铺的访问权限。请核对店铺并使用对应店主账号。", "This account cannot access that shop. Check the shop and sign in as its owner.");
      if (error?.status === 409 || /409|digest|changed|revision|核对|变更/i.test(message)) return tr("商品或授权申请已变化。请刷新页面，重新核对后提交。", "The products or authorization request changed. Refresh and review before submitting.");
      return tr("操作未能确认。请刷新检查结果后重试。", "The operation could not be confirmed. Refresh to check the result before retrying.");
    };
    const bind = (name, event, handler) => $(name)?.addEventListener(event, (value) => { if (!current()) return; value.preventDefault(); return handler(value); });
    const controls = (disabled) => root.querySelectorAll?.("[data-commerce-control]").forEach((node) => { node.disabled = disabled; });
    function render(body, authorization = false) {
      if (!structural()) return;
      generation++;
      root.innerHTML = `<section id="${prefix}-surface" class="commerce-connect${authorization ? " commerce-authorization" : ""}">${body}<p id="${prefix}-status" class="commerce-status" role="status" aria-live="polite"></p><p id="${prefix}-error" class="error" role="alert"></p></section>`;
      marker = $("surface");
    }
    async function run(operation) {
      if (!current() || busy) return;
      busy = true; controls(true); setError("");
      const version = generation, shopId = selectedShop;
      try { await operation(version, shopId); }
      catch (error) { if (alive(version, shopId)) setError(errorMessage(error)); }
      finally { busy = false; if (current()) controls(false); }
    }
    async function checkSession(version, shopId) {
      const value = await request("/auth/status");
      if (!alive(version, shopId)) return false;
      if (!owner(value) || identity(value) !== initialIdentity || value.session_id !== sessionId) throw new Error("Browser session changed");
      return true;
    }
    async function fresh(title, operation, version, shopId) {
      if (typeof reauthenticate !== "function" || !alive(version, shopId)) throw new Error("Fresh verification unavailable");
      const adopt = (value) => {
        if (!structural() || generation !== version || selectedShop !== shopId || !owner(value) || identity(value) !== initialIdentity) throw new Error("Browser identity changed");
        sessionId = value.session_id; currentAuth = value; if (typeof auth !== "function") auth = value; onAuth(value);
      };
      const confirmation = $("confirmation");
      const pending = reauthenticate({ root: confirmation, title, isCurrent: () => structural() && generation === version && selectedShop === shopId && identity(readAuth()) === initialIdentity, onAuth: adopt });
      confirmation?.scrollIntoView?.({ block: "start", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      confirmation?.querySelector?.('input[type="password"], button')?.focus?.({ preventScroll: true });
      const verified = await pending;
      if (verified === false || verified === null || verified === undefined) { if (alive(version, shopId)) setStatus(tr("已取消，尚未提交。", "Cancelled; nothing was submitted.")); return; }
      if (typeof verified === "object") adopt(verified);
      if (!alive(version, shopId) || !await checkSession(version, shopId)) return;
      await operation();
    }
    function shopScope() {
      return currentAuth?.superadmin === true
        ? `<div class="field commerce-scope"><label for="${prefix}-shop">${tr("所属店铺", "Shop")}</label><select id="${prefix}-shop" data-commerce-control><option value="">${tr("先选择店铺", "Select a shop first")}</option>${shops.map((shop) => `<option value="${escape(shop.id)}" ${shop.id === selectedShop ? "selected" : ""}>${escape(shop.name)}</option>`).join("")}</select></div>`
        : `<div class="commerce-scope"><p><strong>${escape(currentAuth.shop_name || tr("当前店铺", "Current shop"))}</strong>${currentAuth.shop_email ? `<span class="caption">${escape(currentAuth.shop_email)}</span>` : ""}</p></div>`;
    }
    function bindShop() { bind("shop", "change", async () => { if (busy) return; const value = $("shop").value; if (value && !shops.some((shop) => shop.id === value)) return; selectedShop = value; review = null; selection = null; if (mode === "management") await loadManagement(); else await loadAuthorization(); }); }
    function denied() {
      const loggedIn = !!readAuth()?.session_id;
      render(`<div class="commerce-head"><div><h2>${mode === "authorize" ? tr("连接上游商城", "Connect a marketplace") : tr("商城连接", "Marketplace connections")}</h2><p class="caption">${loggedIn ? tr("商品管理链接不能批准商城访问。请使用店主账号或平台管理员账号。", "Product management links cannot approve marketplace access. Use a shop owner or platform administrator account.") : tr("先登录店主账号，再核对商城、商品和授权范围。登录本身不会批准申请。", "Sign in as a shop owner, then review the marketplace, products and access. Signing in does not approve the request.")}</p></div></div><a id="${prefix}-login" class="commerce-login" href="${mode === "authorize" && requestIdentifier.test(requestId) ? "/account/login?return_to=" + encodeURIComponent("/connect/authorize#" + requestId) : "/account/login"}">${tr("登录店主账号", "Sign in as shop owner")}</a>`, mode === "authorize");
    }
    function loading(authorization = false) {
      render(`<div class="commerce-head"><div><h2>${authorization ? tr("连接上游商城", "Connect a marketplace") : tr("商城连接", "Marketplace connections")}</h2></div></div><div class="commerce-loading" role="status"><p>${tr("正在加载可授权的信息…", "Loading the available authorization details…")}</p><div></div><div></div></div>`, authorization);
    }
    function validateShops(value) {
      if (!Array.isArray(value) || value.length > 1000 || !unique(value.map((shop) => shop?.id)) || value.some((shop) => !uuid.test(shop.id || "") || !text(shop.name))) throw new Error("Invalid shop list");
      return value.filter((shop) => shop.enabled !== false && shop.enabled !== 0);
    }
    function validateClients(value) {
      if (!Array.isArray(value?.clients) || value.clients.length > 2000 || !unique(value.clients.map((client) => client?.client_id))) throw new Error("Invalid clients");
      for (const client of value.clients) {
        if (!identifier.test(client.client_id || "") || !text(client.client_name, 100) || client.shop_id !== selectedShop || !unique(client.redirect_uris) || !client.redirect_uris.length || client.redirect_uris.length > 5 || ![true, false, 0, 1].includes(client.enabled)) throw new Error("Invalid client metadata");
        client.redirect_uris.forEach(callbackURL);
      }
      return structuredClone(value.clients);
    }
    function validateGrants(value) {
      if (!Array.isArray(value?.grants) || value.grants.length > 2000 || !unique(value.grants.map((grant) => grant?.id))) throw new Error("Invalid grants");
      for (const grant of value.grants) {
        if (!uuid.test(grant.id || "") || !identifier.test(grant.client_id || "") || !text(grant.client_name, 100) || grant.shop_id !== selectedShop || !Number.isFinite(grant.expires)
          || !unique(grant.scopes) || !grant.scopes.length || grant.scopes.some((scope) => !allowedScopes.has(scope)) || !unique(grant.product_ids) || grant.product_ids.some((id) => !productIdentifier.test(id))
          || !Array.isArray(grant.products) || grant.products.length > 100 || grant.products.some((product) => !productIdentifier.test(product.id || "") || !text(product.name)) || !Array.isArray(grant.card_limits) || grant.card_limits.length > 500) throw new Error("Invalid grant metadata");
        for (const limit of grant.card_limits) if (!productIdentifier.test(limit.product_id || "") || !identifier.test(limit.variant_id || "") || !Number.isSafeInteger(limit.max_count) || limit.max_count < 1 || !Number.isSafeInteger(limit.issued_count) || limit.issued_count < 0 || !Number.isSafeInteger(limit.remaining) || limit.remaining < 0 || limit.issued_count + limit.remaining !== limit.max_count) throw new Error("Invalid grant limits");
      }
      return structuredClone(value.grants);
    }
    const scopeLabel = (scope) => scope === "products.read" ? tr("读取商品详情与规格", "Read product details and variants") : tr("按授权额度发行新卡密", "Issue new codes within approved limits");
    const rowStatus = (item) => item.revoked || item.enabled === false || item.enabled === 0 ? tr("已撤销", "Revoked") : item.expires && item.expires <= now() ? tr("已到期", "Expired") : tr("有效", "Active");
    function managementPage() {
      const displayedClients = clients.filter((client) => showHistory || (client.enabled !== false && client.enabled !== 0));
      const displayedGrants = grants.filter((grant) => showHistory || (!grant.revoked && grant.expires > now()));
      render(`<div class="commerce-head"><div><h2>${tr("商城连接", "Marketplace connections")}</h2><p class="caption">${tr("让上游商城导入商品详情，并在你批准的规格额度内补充新卡密。", "Let a marketplace import product details and replenish new codes within your approved variant limits.")}</p></div><a href="${docs}" target="_blank" rel="noopener noreferrer">${tr("开放协议接入文档", "Open protocol integration guide")}</a></div>${shopScope()}${selectedShop ? `<div class="commerce-columns"><section class="commerce-registration"><details ${displayedClients.length ? "" : "open"}><summary>${tr("注册商城应用", "Register a marketplace app")}</summary><p class="caption">${tr("填写你要连接的商城名称和回调地址。应用使用 PKCE，不生成共享密钥；注册之后仍需逐次批准商品与制卡额度。", "Enter the marketplace name and callback URLs. Apps use PKCE without a shared secret; product access and code issuance limits still require separate approval.")}</p><form id="${prefix}-register"><div class="field"><label for="${prefix}-client-name">${tr("商城应用名称", "Marketplace app name")}</label><input id="${prefix}-client-name" data-commerce-control type="text" maxlength="100" required autocomplete="off"></div><div class="field"><label for="${prefix}-redirects">${tr("HTTPS 回调地址", "HTTPS callback URLs")}</label><textarea id="${prefix}-redirects" data-commerce-control rows="3" maxlength="10244" required spellcheck="false" autocomplete="off" placeholder="https://store.example/connect/callback" aria-describedby="${prefix}-redirect-help"></textarea><p id="${prefix}-redirect-help" class="caption">${tr("每行一个，最多 5 个。必须精确匹配，包括路径；不能有 fragment 或 OAuth 保留参数。", "One per line, up to 5. The exact URL, including its path, must match. Fragments and reserved OAuth parameters are not allowed.")}</p></div><button data-commerce-control type="submit">${tr("验证身份并注册应用", "Verify identity and register app")}</button></form></details><div id="${prefix}-registered" hidden class="commerce-review"></div></section><div><section><div class="commerce-list-head"><h3>${tr("已注册的应用", "Registered apps")}</h3><button id="${prefix}-refresh" data-commerce-control type="button" class="secondary">${tr("刷新", "Refresh")}</button></div><div>${displayedClients.map((client, index) => `<article class="commerce-row"><div class="commerce-row-header"><div><h4>${escape(client.client_name)}</h4><p class="caption">${rowStatus(client)} · ${tr("公开客户端 · PKCE", "Public client · PKCE")}</p></div>${client.enabled !== false && client.enabled !== 0 ? `<button id="${prefix}-client-revoke-${index}" data-commerce-control type="button" class="secondary">${tr("撤销应用", "Revoke app")}</button>` : ""}</div><details><summary>${tr("客户端标识与回调地址", "Client identifier and callback URLs")}</summary><div class="field"><label for="${prefix}-client-id-${index}">client_id</label><input id="${prefix}-client-id-${index}" type="text" value="${escape(client.client_id)}" readonly spellcheck="false"><button id="${prefix}-client-copy-${index}" data-commerce-control type="button" class="secondary">${tr("复制标识", "Copy identifier")}</button></div><dl><dt>${tr("回调地址", "Callback URLs")}</dt><dd>${client.redirect_uris.map(escape).join("<br>")}</dd></dl></details></article>`).join("") || `<div class="commerce-empty"><strong>${tr("还没有连接商城", "No marketplace connection yet")}</strong><p>${tr("先注册商城应用，把客户端标识交给商城，再从商城发起授权。", "Register an app first, provide its client identifier to the marketplace, then start authorization there.")}</p></div>`}</div></section><section class="commerce-grants"><div class="commerce-list-head"><h3>${tr("商品访问授权", "Product access grants")}</h3></div>${displayedGrants.map((grant, index) => `<article class="commerce-row"><div class="commerce-row-header"><div><h4>${escape(grant.client_name)}</h4><p class="caption">${rowStatus(grant)} · ${tr("到期", "Expires")} ${escape(date(grant.expires))}</p></div>${!grant.revoked && grant.expires > now() ? `<button id="${prefix}-grant-revoke-${index}" data-commerce-control type="button" class="secondary">${tr("撤销授权", "Revoke access")}</button>` : ""}</div><p class="caption">${grant.scopes.map(scopeLabel).map(escape).join(" · ")}</p><details><summary>${tr("查看已批准商品与剩余额度", "View approved products and remaining limits")}</summary><ul class="commerce-summary-list">${grant.products.map((product) => `<li>${escape(product.name)}</li>`).join("")}</ul>${grant.card_limits.length ? `<dl>${grant.card_limits.map((limit) => `<dt>${escape(grant.products.find((product) => product.id === limit.product_id)?.name || limit.product_id)} · ${escape(limit.variant_id)}</dt><dd>${tr("剩余", "Remaining")} ${limit.remaining} / ${limit.max_count}</dd>`).join("")}</dl>` : `<p class="caption">${tr("未授予制卡权限。", "Code issuance is not authorized.")}</p>`}</details></article>`).join("") || `<p class="commerce-empty caption">${tr("目前没有有效授权。商城发起连接后，你可以逐项批准商品和权限。", "No active grants. When a marketplace starts connecting, you can approve products and permissions individually.")}</p>`}</section><div class="commerce-review"><label class="commerce-choice" for="${prefix}-history"><input id="${prefix}-history" data-commerce-control type="checkbox" ${showHistory ? "checked" : ""}><span>${tr("显示已撤销与到期的历史", "Show revoked and expired history")}</span></label></div></div></div>` : `<p class="commerce-empty caption">${tr("选择一个店铺，再注册商城应用或查看授权。", "Select a shop to register an app or review access grants.")}</p>`}<div id="${prefix}-revoke-review"></div><div id="${prefix}-confirmation"></div>`);
      bindShop(); bind("refresh", "click", () => loadManagement());
      bind("history", "change", () => { if (busy) return; showHistory = $("history").checked; return loadManagement(); });
      bind("register", "submit", () => run(registerClient));
      displayedClients.forEach((client, index) => {
        bind("client-copy-" + index, "click", () => copyIdentifier($("client-id-" + index), client.client_id));
        bind("client-revoke-" + index, "click", () => revokeReview("clients", client.client_id, client.client_name));
      });
      displayedGrants.forEach((grant, index) => bind("grant-revoke-" + index, "click", () => revokeReview("grants", grant.id, grant.client_name)));
    }
    async function copyIdentifier(input, value) {
      if (!current() || !input || input.isConnected === false) return;
      const version = generation, shopId = selectedShop; let copied = false;
      try { copied = typeof window.ExtoreClipboard?.writeText === "function" ? await window.ExtoreClipboard.writeText(value) : (await navigator.clipboard.writeText(value), true); } catch { /* Keep the public identifier selectable. */ }
      if (!alive(version, shopId) || input.isConnected === false) return;
      if (copied) setStatus(tr("客户端标识已复制。", "Client identifier copied."));
      else { input.focus(); input.select(); setStatus(tr("复制失败，已选中客户端标识，请手动复制。", "Copy failed. The client identifier is selected for manual copying.")); }
    }
    async function registerClient(version, shopId) {
      const name = $("client-name").value.trim(), redirects = $("redirects").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      if (!text(name, 100) || !unique(redirects) || !redirects.length || redirects.length > 5) { setError(tr("填写应用名称，并提供 1 至 5 个不同的 HTTPS 回调地址。", "Enter an app name and 1 to 5 distinct HTTPS callback URLs.")); return; }
      try { for (const value of redirects) { const url = callbackURL(value); for (const key of ["code", "state", "iss", "error", "error_description"]) if (url.searchParams.has(key)) throw new Error("Reserved query"); } }
      catch { setError(tr("回调地址必须是完整 HTTPS 地址，不能包含 fragment、账号密码或 OAuth 保留参数。", "Use complete HTTPS callbacks without fragments, credentials or reserved OAuth parameters.")); return; }
      await fresh(tr("确认注册商城应用", "Confirm marketplace app registration"), async () => {
        const result = await request(scoped("/admin/commerce/clients"), { client_name: name, redirect_uris: redirects, token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] });
        if (!alive(version, shopId)) return;
        const returned = result?.client || result;
        const items = validateClients({ clients: [returned] });
        if (items[0].client_name !== name || JSON.stringify(items[0].redirect_uris) !== JSON.stringify(redirects)) throw new Error("Registered client metadata changed");
        await loadManagement(); if (current()) setStatus(tr("应用已注册。展开客户端标识复制给商城，再由商城发起授权。", "App registered. Expand its client identifier and copy it to the marketplace, then start authorization there."));
      }, version, shopId);
    }
    function revokeReview(kind, id, name) {
      if (!current() || busy) return;
      const target = $("revoke-review"), version = generation, shopId = selectedShop;
      target.innerHTML = `<section class="commerce-review"><h3>${kind === "clients" ? tr("撤销这个商城应用？", "Revoke this marketplace app?") : tr("撤销这次商品授权？", "Revoke this product access grant?")}</h3><p>${escape(name)}</p><p class="caption">${kind === "clients" ? tr("同时撤销它的全部授权，后续导入和补货请求将被拒绝。已导入到商城的资料和已发行卡密不会被删除。", "All of this app's access grants will also be revoked. Further import and replenishment requests will be rejected. Existing marketplace data and issued codes are preserved.") : tr("后续导入和补货请求将被拒绝。已导入资料和已发行卡密保留。", "Further import and replenishment requests will be rejected. Existing imported data and issued codes are preserved.")}</p><div class="commerce-actions"><button id="${prefix}-revoke-confirm" data-commerce-control type="button" class="danger">${tr("验证身份并撤销", "Verify identity and revoke")}</button><button id="${prefix}-revoke-cancel" data-commerce-control type="button" class="secondary">${tr("取消", "Cancel")}</button></div></section>`;
      bind("revoke-cancel", "click", () => { if (!busy && alive(version, shopId)) target.innerHTML = ""; });
      bind("revoke-confirm", "click", () => run(async () => {
        await fresh(tr("确认撤销商城访问", "Confirm marketplace access revocation"), async () => {
          const result = await request(scoped("/admin/commerce/" + kind + "/" + encodeURIComponent(id)), undefined, "DELETE");
          if (!alive(version, shopId)) return;
          if (result?.ok !== true) throw new Error("Unconfirmed revocation");
          await loadManagement(); if (current()) setStatus(tr("已撤销。", "Revoked."));
        }, version, shopId);
      }));
      target.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    }
    async function loadManagement() {
      if (!current()) return; loading();
      const version = generation, shopId = selectedShop;
      try {
        if (currentAuth.superadmin === true && !shops.length) { shops = validateShops(await request("/platform/shops")); if (!alive(version, shopId)) return; }
        if (selectedShop) {
          const values = await Promise.all([request(scoped("/admin/commerce/clients", { view: showHistory ? "all" : "active" })), request(scoped("/admin/commerce/grants", { view: showHistory ? "all" : "active" }))]);
          if (!alive(version, shopId)) return; clients = validateClients(values[0]); grants = validateGrants(values[1]);
        } else { clients = []; grants = []; }
        if (alive(version, shopId)) managementPage();
      } catch (error) { if (alive(version, shopId)) { managementPage(); setError(errorMessage(error)); } }
    }
    function application(value) {
      return `<div class="commerce-application"><h3>${escape(value.request.client_name)}</h3><p class="caption">${tr("应用名称由申请者自行填写，Extore 未验证它的企业身份。请与商城显示的信息核对。", "The requester supplied this app name. Extore has not verified its business identity; compare it with the marketplace.")}</p><p class="commerce-host">${escape(value.request.redirect_host)}</p><p class="caption">${tr("完成后只会返回这个已注册的回调地址。", "After completion, you will return only to this registered callback URL.")}</p><details><summary>${tr("查看完整回调地址与客户端标识", "View the complete callback and client identifier")}</summary><dl><dt>client_id</dt><dd class="mono">${escape(value.request.client_id)}</dd><dt>${tr("回调地址", "Callback URL")}</dt><dd>${escape(value.request.redirect_uri)}</dd><dt>${tr("本次申请到期", "Request expires")}</dt><dd>${escape(date(value.request.expires))}</dd></dl></details></div>`;
    }
    const steps = (step) => `<ol class="commerce-steps" aria-label="${tr("授权步骤", "Authorization steps")}"><li ${step === 1 ? 'aria-current="step"' : ""}>${tr("核对商城", "Check marketplace")}</li><li ${step === 2 ? 'aria-current="step"' : ""}>${tr("限定范围", "Set access limits")}</li><li ${step === 3 ? 'aria-current="step"' : ""}>${tr("验证并批准", "Verify and approve")}</li></ol>`;
    function authorizationShell(body, step) {
      render(`<div class="commerce-head"><div><h2>${tr("连接上游商城", "Connect a marketplace")}</h2><p class="caption">${tr("导入商品资料与补充卡密，支付和订单仍由商城处理。", "Import product information and replenish codes. The marketplace handles payments and orders.")}</p></div></div>${shopScope()}<div class="commerce-paper">${steps(step)}${body}<div id="${prefix}-confirmation"></div></div>`, true);
      bindShop();
    }
    function selectionPage(value, message = "", previous = null) {
      review = value; selection = null;
      const candidates = value.request.requested_product_ids.length ? value.products.filter((product) => value.request.requested_product_ids.includes(product.id)) : value.products;
      const defaultExpires = previous?.grant_expires > now() && previous.grant_expires <= value.request.max_grant_expires ? previous.grant_expires : Math.min(now() + 30 * 86400, value.request.max_grant_expires);
      const localTime = (seconds) => { const dateValue = new Date(seconds * 1000); return new Date(dateValue.getTime() - dateValue.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
      authorizationShell(`${application(value)}<div class="commerce-separator"><form id="${prefix}-selection"><fieldset><legend>${tr("批准哪些当前商品", "Approve current products")}</legend><p class="caption">${tr("只包含你选中的当前商品。以后新建的商品不会自动加入。", "Only selected current products are included. Future products are not added automatically.")}</p><div class="commerce-products">${candidates.map((product, index) => `<label class="commerce-choice" for="${prefix}-product-${index}"><input id="${prefix}-product-${index}" data-commerce-control data-commerce-product="${escape(product.id)}" type="checkbox" value="${escape(product.id)}"><span>${escape(product.name)}<small>${product.variants.filter((variant) => variant.enabled !== false && variant.enabled !== 0).map((variant) => escape(variant.name)).join(" · ") || tr("没有启用的规格", "No enabled variants")}</small></span></label>`).join("") || `<p class="caption">${tr("没有可批准的商品。先在后台创建商品，再重新发起商城连接。", "No products are available. Create a product in the dashboard, then start a new marketplace connection.")}</p>`}</div>${candidates.length > 1 ? `<button id="${prefix}-select-all" data-commerce-control type="button" class="secondary">${tr("选择这些当前商品", "Select these current products")}</button>` : ""}</fieldset><fieldset class="commerce-separator"><legend>${tr("授予哪些权限", "Grant permissions")}</legend>${value.request.scopes.map((scope, index) => `<label class="commerce-choice" for="${prefix}-scope-${index}"><input id="${prefix}-scope-${index}" data-commerce-control data-commerce-scope="${escape(scope)}" type="checkbox" value="${escape(scope)}" ${scope === "products.read" ? "checked" : ""}><span>${escape(scopeLabel(scope))}<small>${scope === "products.read" ? tr("包括标题、描述、图片、规格和参考价。不会读取任务、顾客资料或现有卡密。", "Includes titles, descriptions, images, variants and reference prices. No tasks, customer data or existing codes.") : tr("只能生成新的卡密。每个规格有本次授权的总额度，补货会消耗额度。", "Only new codes can be generated. Each variant has a total issuance limit; replenishment consumes it.")}</small></span></label>`).join("")}</fieldset><section id="${prefix}-limits" class="commerce-limits" hidden><h3>${tr("每个规格的制卡总额度", "Total code issuance limit per variant")}</h3><p class="caption">${tr("填写本次授权最多能生成多少张卡密。0 表示不批准这个规格；此处不是商城的销售库存。", "Set the maximum codes this grant may generate. 0 excludes a variant. These limits are not marketplace sales stock.")}</p><div id="${prefix}-limit-rows"></div><p id="${prefix}-limit-total" class="commerce-limit-total" role="status"></p></section><div class="field commerce-separator"><label for="${prefix}-expires">${tr("授权到期时间（本机时区）", "Access expires (local time zone)")}</label><input id="${prefix}-expires" data-commerce-control type="datetime-local" value="${escape(localTime(defaultExpires))}" max="${escape(localTime(value.request.max_grant_expires))}" required><p class="caption">${tr("可以缩短期限，不能超过商城申请的上限。你可以随时在后台撤销。", "You can shorten the term, within the requested maximum. Revoke access anytime from the dashboard.")}</p></div><div class="commerce-actions"><button id="${prefix}-review" data-commerce-control type="submit">${tr("核对授权范围", "Review access scope")}</button><button id="${prefix}-deny" data-commerce-control type="button" class="secondary">${tr("拒绝连接", "Deny connection")}</button></div></form></div>`, 2);
      const update = () => updateLimits(value, candidates);
      if (previous) {
        root.querySelectorAll("[data-commerce-product]").forEach((node) => { node.checked = previous.product_ids.includes(node.value); });
        root.querySelectorAll("[data-commerce-scope]").forEach((node) => { node.checked = previous.scopes.includes(node.value); });
      }
      root.querySelectorAll?.("[data-commerce-product], [data-commerce-scope]").forEach((node) => node.addEventListener("change", () => { if (current() && !busy) update(); }));
      bind("select-all", "click", () => { if (busy) return; root.querySelectorAll("[data-commerce-product]").forEach((node) => { node.checked = true; }); update(); });
      bind("selection", "submit", () => { if (busy) return; try { const selected = captureSelection(value, candidates); confirmPage(value, selected); } catch { setError(tr("请选择至少一个商品和权限，核对有效期与各规格的整数制卡额度。", "Select at least one product and permission, then check the expiration and integer issuance limits.")); } });
      bind("deny", "click", () => { let chosen = null; try { chosen = captureSelection(value, candidates); } catch { /* A denial need not have a valid grant selection. */ } denyReview(value, chosen); });
      update();
      if (previous) {
        root.querySelectorAll("[data-commerce-limit]").forEach((node) => { const limit = previous.card_limits.find((item) => item.product_id === node.dataset.commerceProductId && item.variant_id === node.dataset.commerceVariantId); node.value = String(limit?.max_count ?? 0); });
        root.querySelectorAll("[data-commerce-limit]")[0]?.dispatchEvent?.(new Event("input"));
      }
      if (message) setError(message);
    }
    function updateLimits(value, candidates) {
      const enabled = root.querySelectorAll("[data-commerce-scope]");
      const issuing = [...enabled].some((node) => node.checked && node.value === "cards.issue");
      const existing = new Map([...root.querySelectorAll("[data-commerce-limit]")].map((node) => [node.dataset.commerceLimit, node.value]));
      const ids = [...root.querySelectorAll("[data-commerce-product]")].filter((node) => node.checked).map((node) => node.value);
      $("limits").hidden = !issuing;
      const rows = candidates.filter((product) => ids.includes(product.id)).flatMap((product) => product.variants.filter((variant) => variant.enabled !== false && variant.enabled !== 0).map((variant) => ({ product, variant })));
      $("limit-rows").innerHTML = rows.map(({ product, variant }, index) => { const key = product.id + "/" + variant.id; return `<div class="commerce-limit-row"><label for="${prefix}-limit-${index}">${escape(product.name)}<small>${escape(variant.name)}</small></label><input id="${prefix}-limit-${index}" data-commerce-control data-commerce-limit="${escape(key)}" data-commerce-product-id="${escape(product.id)}" data-commerce-variant-id="${escape(variant.id)}" type="number" min="0" max="10000" step="1" inputmode="numeric" value="${escape(existing.get(key) ?? "10")}" aria-label="${escape(product.name + " · " + variant.name + " · " + tr("制卡总额度", "Total issuance limit"))}"></div>`; }).join("") || `<p class="caption">${tr("先选择含启用规格的商品。", "First select a product with enabled variants.")}</p>`;
      const total = () => {
        if (!current()) return;
        const counts = [...root.querySelectorAll("[data-commerce-limit]")].map((node) => Number(node.value));
        $("limit-total").textContent = counts.every((count) => Number.isSafeInteger(count) && count >= 0 && count <= 10000) ? tr(`本次授权总额度：${counts.reduce((sum, count) => sum + count, 0)} 张`, `Total limit for this grant: ${counts.reduce((sum, count) => sum + count, 0)} codes`) : tr("请填写 0 至 10000 的整数。", "Enter integers between 0 and 10000.");
      };
      root.querySelectorAll("[data-commerce-limit]").forEach((node) => node.addEventListener("input", total)); total();
    }
    function captureSelection(value, candidates) {
      if (value.request.expires <= now()) throw new Error("Expired request");
      const productIds = [...root.querySelectorAll("[data-commerce-product]")].filter((node) => node.checked).map((node) => node.value);
      const scopes = [...root.querySelectorAll("[data-commerce-scope]")].filter((node) => node.checked).map((node) => node.value);
      const expires = Math.floor(new Date($("expires").value).getTime() / 1000);
      if (!productIds.length || !unique(productIds) || productIds.some((id) => !candidates.some((product) => product.id === id)) || !scopes.length || !unique(scopes) || scopes.some((scope) => !value.request.scopes.includes(scope)) || !Number.isFinite(expires) || expires <= now() || expires > value.request.max_grant_expires) throw new Error("Invalid selection");
      const limits = scopes.includes("cards.issue") ? [...root.querySelectorAll("[data-commerce-limit]")].map((node) => ({ product_id: node.dataset.commerceProductId, variant_id: node.dataset.commerceVariantId, max_count: Number(node.value) })).filter((limit) => limit.max_count !== 0) : [];
      if (scopes.includes("cards.issue") && (!limits.length || limits.length > 500)) throw new Error("Invalid number of issuance limits");
      for (const limit of limits) if (!Number.isSafeInteger(limit.max_count) || limit.max_count < 1 || limit.max_count > 10000 || !productIds.includes(limit.product_id) || !candidates.find((product) => product.id === limit.product_id)?.variants.some((variant) => variant.id === limit.variant_id && variant.enabled !== false && variant.enabled !== 0)) throw new Error("Invalid limit");
      return { shop_id: selectedShop, review_digest: value.review_digest, scopes, product_ids: productIds, card_limits: limits, grant_expires: expires };
    }
    function confirmPage(value, selected) {
      review = value; selection = structuredClone(selected);
      authorizationShell(`${application(value)}<div class="commerce-separator"><h3>${tr("确认本次授权", "Confirm this authorization")}</h3><p>${escape(value.shop.name)}</p><ul class="commerce-summary-list">${selected.product_ids.map((id) => `<li>${escape(value.products.find((product) => product.id === id)?.name)}</li>`).join("")}</ul><dl><dt>${tr("权限", "Permissions")}</dt><dd>${selected.scopes.map(scopeLabel).map(escape).join("<br>")}</dd><dt>${tr("授权到期", "Access expires")}</dt><dd>${escape(date(selected.grant_expires))}</dd></dl>${selected.card_limits.length ? `<h3>${tr("本次批准的制卡额度", "Approved code issuance limits")}</h3><dl>${selected.card_limits.map((limit) => { const product = value.products.find((item) => item.id === limit.product_id), variant = product?.variants.find((item) => item.id === limit.variant_id); return `<dt>${escape(product?.name)} · ${escape(variant?.name)}</dt><dd>${limit.max_count} ${tr("张", "codes")}</dd>`; }).join("")}</dl>` : `<p class="caption">${tr("仅导入资料，不批准生成卡密。", "Import information only; code issuance is not approved.")}</p>`}<p class="caption">${tr("新商品不会自动加入，超出额度的补货请求会被拒绝。接下来需要新的 Passkey 或密码与 2FA 验证。", "New products are not added automatically, and over-limit replenishment is rejected. Next, verify again with a Passkey or password and 2FA.")}</p><div class="commerce-actions"><button id="${prefix}-approve" data-commerce-control type="button">${tr("重新验证并批准", "Verify again and approve")}</button><button id="${prefix}-adjust" data-commerce-control type="button" class="secondary">${tr("调整范围", "Adjust scope")}</button><button id="${prefix}-deny" data-commerce-control type="button" class="secondary">${tr("拒绝连接", "Deny connection")}</button></div></div>`, 3);
      bind("adjust", "click", () => { if (!busy) selectionPage(value, "", selected); });
      bind("deny", "click", () => denyReview(value, selected));
      bind("approve", "click", () => run(async (version, shopId) => {
        await fresh(tr("确认商城商品授权", "Confirm marketplace product access"), async () => {
          const latest = context(await request(scoped("/admin/commerce/requests/" + encodeURIComponent(requestId))), shopId, requestId);
          if (!alive(version, shopId)) return;
          if (latest.review_digest !== value.review_digest || JSON.stringify(latest) !== JSON.stringify(value)) { selectionPage(latest, tr("授权信息已变化，请重新选择并核对。", "Authorization details changed. Select and review again.")); return; }
          if (selected.grant_expires <= now() || value.request.expires <= now()) throw new Error("Expired authorization");
          const result = await request(scoped("/admin/commerce/requests/" + encodeURIComponent(requestId) + "/approve"), structuredClone(selected));
          if (!alive(version, shopId)) return;
          const destination = safeRedirect(result, value.request.redirect_uri, "approved");
          render(`<div class="commerce-paper"><h2>${tr("授权已完成", "Access approved")}</h2><p role="status">${tr("正在返回你核对过的商城。", "Returning to the marketplace you reviewed.")}</p></div>`, true);
          navigate(destination);
        }, version, shopId);
      }));
    }
    function denyReview(value, previous = null) {
      if (!current() || busy) return;
      review = value; selection = null;
      authorizationShell(`${application(value)}<div class="commerce-separator"><h3>${tr("拒绝这次连接？", "Deny this connection?")}</h3><p>${tr("不会授予商品读取或制卡权限。验证身份后，会把拒绝结果返回上方商城。", "No product reading or code issuance access will be granted. After identity verification, the denial is returned to the marketplace above.")}</p><div class="commerce-actions"><button id="${prefix}-deny-confirm" data-commerce-control type="button" class="danger">${tr("验证身份并拒绝", "Verify identity and deny")}</button><button id="${prefix}-adjust" data-commerce-control type="button" class="secondary">${tr("返回授权范围", "Back to access scope")}</button></div></div>`, 3);
      bind("adjust", "click", () => { if (!busy) selectionPage(value, "", previous); });
      bind("deny-confirm", "click", () => run(async (version, shopId) => {
        await fresh(tr("确认拒绝商城连接", "Confirm marketplace denial"), async () => {
          const latest = context(await request(scoped("/admin/commerce/requests/" + encodeURIComponent(requestId))), shopId, requestId);
          if (!alive(version, shopId)) return;
          if (latest.review_digest !== value.review_digest || JSON.stringify(latest) !== JSON.stringify(value)) { selectionPage(latest, tr("授权信息已变化，请重新核对。", "Authorization details changed. Review them again.")); return; }
          const result = await request(scoped("/admin/commerce/requests/" + encodeURIComponent(requestId) + "/deny"), { shop_id: shopId, review_digest: latest.review_digest });
          if (!alive(version, shopId)) return;
          const destination = safeRedirect(result, latest.request.redirect_uri, "denied");
          render(`<div class="commerce-paper"><h2>${tr("已拒绝连接", "Connection denied")}</h2><p role="status">${tr("正在返回商城，没有授予权限。", "Returning to the marketplace without granting access.")}</p></div>`, true); navigate(destination);
        }, version, shopId);
      }));
    }
    async function loadAuthorization() {
      if (!current()) return; loading(true);
      const version = generation, shopId = selectedShop;
      try {
        if (!requestIdentifier.test(requestId)) throw new Error("Invalid request ID");
        if (currentAuth.superadmin === true && !shops.length) { shops = validateShops(await request("/platform/shops")); if (!alive(version, shopId)) return; }
        if (!selectedShop) { authorizationShell(`<p class="commerce-empty caption">${tr("选择本次授权的店铺。商城不会获得其他店铺的资料。", "Select the shop for this authorization. Other shops are not included.")}</p>`, 1); return; }
        const value = context(await request(scoped("/admin/commerce/requests/" + encodeURIComponent(requestId))), shopId, requestId);
        if (alive(version, shopId)) selectionPage(value);
      } catch (error) {
        if (!alive(version, shopId)) return;
        authorizationShell(`<p>${tr("无法加载这次商城连接。请回到商城确认申请仍然有效，再重试。", "This marketplace connection could not load. Check that the request is still valid in the marketplace, then retry.")}</p><button id="${prefix}-retry" data-commerce-control type="button" class="secondary">${tr("重新加载", "Reload")}</button>`, 1);
        bind("retry", "click", () => loadAuthorization()); setError(errorMessage(error));
      }
    }
    const leave = () => instance.dispose();
    const instance = Object.freeze({ dispose() { if (disposed) return; disposed = true; generation++; controller.abort(); review = null; selection = null; clients = []; grants = []; shops = []; window.removeEventListener?.("pagehide", leave); if (mounts.get(root) === instance) mounts.delete(root); }, get active() { return current(); }, get state() { return { mode, shopId: selectedShop, reviewing: !!review, confirming: !!selection }; } });
    mounts.set(root, instance); window.addEventListener?.("pagehide", leave);
    if (!owner(currentAuth)) denied();
    else if (mode === "management") void loadManagement();
    else void loadAuthorization();
    return instance;
  }
  window.ExtoreCommerceConnect = Object.freeze({ mount, context, safeRedirect });
})();
