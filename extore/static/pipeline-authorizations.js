"use strict";

(() => {
  const mounts = new WeakMap();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const validAgentType = (value) => value === undefined || value === null || (typeof value === "string" && value.trim().length >= 1 && value.length <= 64 && !/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value));
  const platform = (auth) => auth?.role === "admin" && auth.superadmin === true && auth.shop_id === null;
  const owner = (auth) => auth?.role === "admin" && (platform(auth) || (auth.superadmin !== true && uuid.test(auth.shop_id || ""))) && typeof auth.session_id === "string" && !!auth.session_id && auth.channel !== "cli";
  const authority = (auth) => JSON.stringify([auth?.role, auth?.shop_id, auth?.superadmin === true]);
  const permissionLabels = {
    "queue.view": ["查看商品队列", "View product queues"],
    "queue.process": ["领取任务、更新进度与交付", "Claim tasks, update progress and deliver"],
    "queue.retry": ["允许失败任务重试", "Allow failed tasks to retry"],
    "product.edit": ["修改商品介绍与填写参数", "Edit product details and fields"],
    "fulfillment.configure": ["配置自动发货与重试规则", "Configure fulfillment and retries"],
    "cards.manage": ["发行、查看与撤销卡密", "Issue, inspect and revoke codes"],
    "events.manage": ["查看事件与重新投递", "Inspect and redeliver events"],
    "links.delegate": ["创建与撤销下级管理链接", "Create and revoke delegated links"],
  };

  function mount({ root, api, auth = {}, isCurrent = () => true, language = "zh-CN", onAuth = () => {}, passkey, copyPrompt } = {}) {
    if (!root?.querySelector || typeof api !== "function") throw new TypeError("Pipeline authorization management requires a root and API");
    mounts.get(root)?.dispose();
    auth = { ...auth };
    let disposed = false, busy = false, revision = 0, view = "active", selectedShop = "", rows = [], shops = [], phase = "list", account = null, review = null;
    const controller = new AbortController();
    const active = () => !disposed && root.isConnected !== false && isCurrent();
    const $ = (selector) => root.querySelector(selector);
    const tr = (cn, en) => String(typeof language === "function" ? language() : language).toLowerCase().startsWith("en") ? en : cn;
    const date = (seconds) => new Date(seconds * 1000).toLocaleString(tr("zh-CN", "en-GB"));
    const request = (path, body, method = body ? "POST" : "GET") => api(path, body, method, { signal: controller.signal, expectedScope: platform(auth) ? "platform" : auth.shop_id, expectedSessionId: auth.session_id });
    const message = (selector, text) => { if (active() && $(selector)) $(selector).textContent = text; };
    const errorText = (error) => error?.name === "NotAllowedError" || error?.name === "AbortError" ? tr("身份验证已取消。", "Identity verification cancelled.") : /revision|版本|409/i.test(String(error?.message || "")) ? tr("授权版本已改变，本次未撤销。请刷新列表并重新核对范围。", "The authorization revision changed; this attempt did not revoke access. Refresh the list and review the scope again.") : /scope|session|身份|会话|范围/i.test(String(error?.message || "")) ? tr("登录身份或店铺已改变，请重新打开会话管理。", "Your identity or shop changed. Reopen session management.") : tr("操作未完成，请刷新授权列表后重试。", "The operation did not complete. Refresh the authorization list and retry.");
    const stopAccount = () => { account?.dispose(); account = null; };
    const promptShop = () => platform(auth) ? shops.find((shop) => shop.id === selectedShop && shop.enabled !== false && shop.enabled !== 0)?.id || "" : auth.shop_id;
    const controls = () => {
      for (const selector of ["#pipeline-refresh", "#pipeline-active", "#pipeline-history-load", "#pipeline-history-view", "#pipeline-shop", "#pipeline-confirm-revoke"]) {
        const node = $(selector); if (node) node.disabled = busy || phase === "confirm";
      }
      const copy = $("#pipeline-copy-prompt"); if (copy) copy.disabled = busy || phase !== "list" || !promptShop() || typeof copyPrompt !== "function";
      for (const row of rows) { const button = $("#pipeline-revoke-" + row.id); if (button) button.disabled = busy || phase !== "list"; }
      const cancel = $("#pipeline-cancel-revoke"); if (cancel) cancel.disabled = busy;
    };
    const bind = (selector, event, handler) => $(selector)?.addEventListener(event, (e) => { e.preventDefault(); return handler(e); });
    const run = async (handler) => {
      if (!active() || busy) return;
      busy = true; controls(); message("#pipeline-error", "");
      const version = revision;
      try { await handler(); }
      catch (error) { if (active() && revision === version) { message("#pipeline-error", errorText(error)); if (!rows.length) message("#pipeline-list-status", tr("授权列表尚未加载。", "The authorization list has not loaded.")); } }
      finally { busy = false; if (active()) controls(); }
    };
    async function checkScope() {
      const current = await request("/auth/status");
      if (!active()) return false;
      if (!owner(current) || authority(current) !== authority(auth) || current.session_id !== auth.session_id) throw new Error("Browser session scope changed");
      return true;
    }
    function validateRows(value, shopId = "") {
      if (!Array.isArray(value) || value.length > 200) throw new Error("Invalid authorization list");
      const seen = new Set();
      for (const row of value) {
        if (!row || !uuid.test(row.id || "") || seen.has(row.id) || !uuid.test(row.shop_id || "") || (!platform(auth) && row.shop_id !== auth.shop_id) || (shopId && row.shop_id !== shopId)
          || !["product", "shop.pipeline"].includes(row.kind) || typeof row.shop_name !== "string" || row.shop_name.length > 1000
          || !Array.isArray(row.permissions) || !row.permissions.length || row.permissions.length > 32 || new Set(row.permissions).size !== row.permissions.length || row.permissions.some((permission) => typeof permission !== "string" || !/^[a-z][a-z0-9_.-]{1,64}$/.test(permission))
          || !Array.isArray(row.product_ids) || row.product_ids.length > 500 || new Set(row.product_ids).size !== row.product_ids.length || row.product_ids.some((id) => !uuid.test(id))
          || !Array.isArray(row.products) || row.products.length !== row.product_ids.length || new Set(row.products.map((product) => product?.id)).size !== row.products.length || row.products.some((product) => !product || !row.product_ids.includes(product.id) || typeof product.name !== "string" || product.name.length > 1000 || typeof product.mode !== "string")
          || (row.kind === "product" && row.product_ids.length > 1) || !Number.isFinite(row.expires) || !Number.isFinite(row.created) || !Number.isFinite(row.last_seen)
          || !Number.isSafeInteger(row.revision) || row.revision < 1 || !Number.isSafeInteger(row.bindings_count) || row.bindings_count < 0
          || typeof row.client_name !== "string" || row.client_name.length > 1000 || !validAgentType(row.agent_type) || typeof row.fingerprint !== "string" || !/^[0-9a-f]{64}$/.test(row.fingerprint)
          || ![true, false, 0, 1].includes(row.revoked) || !["root", "shop"].includes(row.issuer_role) || (row.issuer_role === "root" ? row.issuer_shop_id !== null : row.issuer_shop_id !== row.shop_id)) throw new Error("Invalid authorization scope");
        seen.add(row.id);
      }
      return value;
    }
    function validateShops(value) {
      if (!Array.isArray(value) || value.length > 10000 || new Set(value.map((shop) => shop?.id)).size !== value.length || value.some((shop) => !shop || !uuid.test(shop.id || "") || typeof shop.name !== "string" || shop.name.length > 1000 || ![true, false, 0, 1].includes(shop.enabled))) throw new Error("Invalid shop list");
      return value;
    }
    const listPath = (requestedView = view, shopId = selectedShop) => "/admin/pipeline-authorizations?" + new URLSearchParams({ view: requestedView, ...(platform(auth) && shopId ? { shop_id: shopId } : {}), limit: "200" });
    const details = (row) => `<p class="caption">${tr("代理类型（自报，未经验证）", "Agent type (self-reported, unverified)")}：${esc(row.agent_type || tr("未声明", "Not declared"))}</p><dl class="pipeline-details"><dt>${tr("店铺", "Shop")}</dt><dd>${esc(row.shop_name)}<span class="caption mono">${esc(row.shop_id)}</span></dd><dt>${tr("设备指纹", "Device fingerprint")}</dt><dd class="mono pipeline-fingerprint">${esc(row.fingerprint)}</dd><dt>${tr("期限", "Expires")}</dt><dd>${esc(date(row.expires))}</dd><dt>${tr("授权版本", "Authorization revision")}</dt><dd>${row.revision}</dd><dt>${tr("批准来源", "Approved by")}</dt><dd>${row.issuer_role === "root" ? tr("平台管理员", "Platform administrator") : tr("店铺管理者", "Shop owner")}</dd><dt>${tr("绑定数量", "Bindings")}</dt><dd>${row.bindings_count}</dd></dl><h4>${tr("本次授权的商品", "Products in this authorization")}</h4><ul class="pipeline-products">${row.products.map((product) => `<li>${esc(product.name)}<span class="caption mono">${esc(product.id)} · ${esc(product.mode)}</span></li>`).join("")}</ul><h4>${tr("精确权限", "Exact permissions")}</h4><ul class="pipeline-permissions">${row.permissions.map((permission) => `<li>${esc(permissionLabels[permission] ? tr(...permissionLabels[permission]) : permission)}<span class="caption mono">${esc(permission)}</span></li>`).join("")}</ul>`;
    const stateText = (row) => row.revoked ? tr("已撤销", "Revoked") : row.expires * 1000 <= Date.now() ? tr("已到期", "Expired") : tr("有效", "Active");
    const visibleRows = (value) => value.filter((row) => view === "all" || (view === "revoked" ? !!row.revoked : !row.revoked && row.expires * 1000 > Date.now()));
    function renderRows() {
      if (!active()) return;
      $("#pipeline-list").innerHTML = rows.length ? rows.map((row) => `<article class="pipeline-authorization"><div class="section-head"><div>${window.ExtoreWorkerIdentity?.markup({ name: row.client_name, kind: "cli", agent_type: row.agent_type ?? null }, { language: tr("zh-CN", "en") }) || `<h3>${esc(row.client_name || tr("未命名 CLI 设备", "Unnamed CLI device"))}</h3>`}<p class="caption">${row.kind === "shop.pipeline" ? tr("全店流水线 · 商品快照", "Shop pipelines · product snapshot") : tr("单个商品", "Single product")} · ${esc(row.shop_name)} · ${stateText(row)}</p></div>${!row.revoked && row.expires * 1000 > Date.now() ? `<button id="pipeline-revoke-${row.id}" type="button" class="danger">${tr("撤销授权", "Revoke access")}</button>` : ""}</div><p class="caption mono pipeline-fingerprint">${esc(row.fingerprint)}</p><p class="caption">${tr("最近活动", "Last activity")} ${esc(date(row.last_seen))} · ${tr("到期", "Expires")} ${esc(date(row.expires))} · ${tr("版本", "Revision")} ${row.revision}</p><details class="pipeline-scope"><summary>${tr("商品与权限", "Products and permissions")} · ${row.products.length} ${tr("个商品", "products")}</summary>${details(row)}<p class="caption mono">${esc(row.id)}</p><p class="caption">${tr("创建于", "Created")} ${esc(date(row.created))}</p></details></article>`).join("") : `<p class="muted">${view === "active" ? tr("暂无有效的 AI 流水线授权。", "No active AI pipeline authorizations.") : tr("此范围暂无历史授权。", "No authorization history in this scope.")}</p>`;
      message("#pipeline-list-status", `${view === "active" ? tr("有效授权", "Active authorizations") : view === "revoked" ? tr("已撤销授权", "Revoked authorizations") : tr("全部授权", "All authorizations")} · ${rows.length}${rows.length === 200 ? tr("（最多显示 200 条，请按店铺缩小范围）", " (up to 200; narrow by shop)") : ""}`);
      for (const row of rows) bind("#pipeline-revoke-" + row.id, "click", () => { if (!busy && phase === "list") showReview(row); });
      controls();
    }
    async function loadList() {
      const version = revision;
      const value = await request(listPath());
      if (!active() || revision !== version) return;
      rows = visibleRows(validateRows(value, selectedShop)); renderRows();
    }
    function shell() {
      root.innerHTML = `<section class="pipeline-authorizations"><div class="section-head"><div><h2>${tr("AI 流水线授权", "AI pipeline access")}</h2><p class="caption">${tr("只覆盖本次批准的队列商品；新增商品需再次批准。可以让 AI 再次申请商品或更大权限，由你逐次确认。", "Access covers only the queue products approved this time. New products require another approval. AI can request more products or permissions for you to review.")}</p></div><button id="pipeline-refresh" type="button" class="secondary">${tr("刷新授权", "Refresh access")}</button></div>${platform(auth) ? `<div class="field"><label for="pipeline-shop">${tr("店铺范围", "Shop scope")}</label><select id="pipeline-shop"><option value="">${tr("全部店铺（复制提示词前需选店）", "All shops (select one before copying a prompt)")}</option></select></div>` : `<p class="caption">${tr("店铺范围", "Shop scope")}：<span class="mono">${esc(auth.shop_id)}</span></p>`}<div class="pipeline-prompt"><button id="pipeline-copy-prompt" type="button" class="secondary">${tr("复制给 AI 的全店流水线提示词", "Copy shop pipeline prompt for AI")}</button><p class="caption">${tr("提示词不含访问凭证。AI 在 CLI 申请设备码，店长在网页核对商品清单与权限后授权。", "The prompt contains no credentials. AI requests a device code in the CLI; a shop owner approves the product list and permissions in the browser.")}</p><div id="pipeline-prompt-host"></div></div><div class="pipeline-filters"><button id="pipeline-active" type="button" class="secondary">${tr("有效授权", "Active access")}</button><details id="pipeline-history"><summary>${tr("已撤销与历史", "Revoked access and history")}</summary><div class="actions"><label for="pipeline-history-view">${tr("历史范围", "History scope")}</label><select id="pipeline-history-view"><option value="revoked">${tr("已撤销", "Revoked")}</option><option value="all">${tr("全部（包括已到期）", "All (including expired)")}</option></select><button id="pipeline-history-load" type="button" class="secondary">${tr("查看历史", "Show history")}</button></div></details></div><p id="pipeline-list-status" class="caption" role="status">${tr("正在加载授权…", "Loading access…")}</p><div id="pipeline-list"></div><div id="pipeline-revoke-review"></div><p id="pipeline-notice" class="caption" role="status"></p><p id="pipeline-error" class="error" role="alert"></p></section>`;
      bind("#pipeline-refresh", "click", () => run(loadList));
      bind("#pipeline-active", "click", () => run(async () => { view = "active"; $("#pipeline-history").open = false; await loadList(); }));
      bind("#pipeline-history-load", "click", () => run(async () => { const value = $("#pipeline-history-view").value; if (!["all", "revoked"].includes(value)) return; view = value; await loadList(); }));
      bind("#pipeline-shop", "change", () => run(async () => { const value = $("#pipeline-shop").value; if (value && !shops.some((shop) => shop.id === value)) throw new Error("Invalid shop scope"); selectedShop = value; await loadList(); }));
      bind("#pipeline-copy-prompt", "click", () => run(async () => {
        const shopId = promptShop(), version = revision;
        if (!shopId || phase !== "list" || typeof copyPrompt !== "function") return;
        await copyPrompt({ shopId, allPipelines: true, permissions: ["queue.view", "queue.process", "queue.retry"] }, $("#pipeline-prompt-host"), () => active() && revision === version && phase === "list" && promptShop() === shopId);
      }));
    }
    function dismissReview() {
      if (!active()) return;
      revision++; stopAccount(); review = null; phase = "list"; $("#pipeline-revoke-review").innerHTML = ""; controls();
    }
    function showReview(row, text = "") {
      if (!active()) return;
      revision++; stopAccount(); phase = "review"; review = row;
      $("#pipeline-revoke-review").innerHTML = `<section class="panel pipeline-revoke"><h3>${tr("确认撤销此 AI 的授权", "Confirm revoking this AI's access")}</h3><p>${tr("撤销后，这个授权下的商品设备会话将结束，正在处理的任务会释放回队列。其他授权不受影响。", "Revoking access ends the product device sessions in this authorization and returns tasks in progress to the queue. Other authorizations remain active.")}</p><h4>${esc(row.client_name)}</h4>${details(row)}<div class="actions"><button id="pipeline-confirm-revoke" type="button" class="danger">${tr("验证身份并撤销", "Verify identity and revoke")}</button><button id="pipeline-cancel-revoke" type="button" class="secondary">${tr("取消", "Cancel")}</button></div><div id="pipeline-confirmation"></div></section>`;
      message("#pipeline-error", text);
      bind("#pipeline-cancel-revoke", "click", () => { if (!busy) dismissReview(); });
      bind("#pipeline-confirm-revoke", "click", () => run(async () => { if (await checkScope()) confirmOwner(row); }));
      controls(); $("#pipeline-confirm-revoke")?.focus();
    }
    const snapshot = (row) => JSON.stringify([row.id, row.shop_id, row.shop_name, row.kind, [...row.permissions].sort(), [...row.product_ids].sort(), [...row.products].sort((a, b) => a.id.localeCompare(b.id)), row.expires, row.revision, row.client_name, row.agent_type ?? null, row.fingerprint, !!row.revoked, row.issuer_role, row.issuer_shop_id, row.bindings_count]);
    async function revoke(row) {
      const version = revision;
      if (!await checkScope() || revision !== version) return;
      const latestRows = await request(listPath("all", row.shop_id));
      if (!active() || revision !== version) return;
      const latest = validateRows(latestRows, row.shop_id).find((item) => item.id === row.id);
      if (!latest || latest.revoked) { dismissReview(); await loadList(); message("#pipeline-notice", tr("此授权已失效或已撤销，列表已刷新。", "This authorization expired or was revoked. The list was refreshed.")); return; }
      if (snapshot(row) !== snapshot(latest)) { showReview(latest, tr("授权范围已改变，请重新核对后确认撤销。", "Authorization scope changed. Review it again before revoking.")); return; }
      const result = await request("/admin/pipeline-authorizations/" + encodeURIComponent(row.id), { expected_revision: row.revision }, "DELETE");
      if (!active() || revision !== version) return;
      if (result?.ok !== true || result.id !== row.id || !Number.isSafeInteger(result.revoked_bindings) || result.revoked_bindings < 0 || !Number.isSafeInteger(result.released_jobs) || result.released_jobs < 0) throw new Error("Revocation result not confirmed");
      dismissReview();
      message("#pipeline-notice", tr(`已撤销授权，结束 ${result.revoked_bindings} 个商品绑定，释放 ${result.released_jobs} 个任务。`, `Access revoked: ${result.revoked_bindings} product bindings ended and ${result.released_jobs} tasks released.`));
      await loadList();
    }
    function confirmOwner(row) {
      if (!active() || !window.ExtoreAccount?.mount) throw new Error("Account verification unavailable");
      phase = "confirm"; controls();
      const version = revision, previousAuth = auth;
      account = window.ExtoreAccount.mount({ root: $("#pipeline-confirmation"), api, auth, mode: "confirm", passkey, language, isCurrent: () => active() && revision === version && phase === "confirm",
        onAuth(current) {
          if (!active() || revision !== version) return;
          if (!owner(current) || authority(current) !== authority(previousAuth)) throw new Error("Browser session scope changed");
          auth = { ...current }; onAuth(current);
        },
      });
      account.confirmFresh(async () => {
        if (!active() || revision !== version) return;
        busy = true; controls();
        const cancel = $("#pipeline-confirmation")?.querySelector("#account-fresh-cancel"); if (cancel) cancel.disabled = true;
        try { await revoke(row); }
        catch (error) { if (active() && revision === version) showReview(row, errorText(error)); }
        finally { busy = false; if (active()) controls(); }
      }, tr("确认撤销 AI 流水线授权", "Confirm revoking AI pipeline access"));
      $("#pipeline-confirmation")?.querySelector("#account-fresh-cancel")?.addEventListener("click", () => {
        if (!busy && active() && revision === version) showReview(row, tr("已取消撤销。", "Revocation cancelled."));
      });
    }
    const leave = () => instance.dispose();
    const instance = Object.freeze({
      dispose() { if (disposed) return; disposed = true; revision++; controller.abort(); stopAccount(); review = null; rows = []; window.removeEventListener?.("pagehide", leave); if (mounts.get(root) === instance) mounts.delete(root); },
      refresh() { return owner(auth) && phase === "list" ? run(loadList) : Promise.resolve(); },
      get active() { return active(); }, get view() { return view; }, get phase() { return phase; },
    });
    mounts.set(root, instance); window.addEventListener?.("pagehide", leave);
    if (!owner(auth)) root.innerHTML = `<p class="error" role="alert">${tr("请使用店铺管理者或平台管理员的浏览器会话查看流水线授权。", "Use a shop owner or platform administrator browser session to manage pipeline access.")}</p>`;
    else {
      shell();
      run(async () => {
        const version = revision;
        const values = await Promise.all([request(listPath()), ...(platform(auth) ? [request("/platform/shops")] : [])]);
        if (!active() || revision !== version) return;
        rows = visibleRows(validateRows(values[0]));
        if (platform(auth)) {
          shops = validateShops(values[1]);
          $("#pipeline-shop").innerHTML = `<option value="">${tr("全部店铺（复制提示词前需选店）", "All shops (select one before copying a prompt)")}</option>${shops.map((shop) => `<option value="${shop.id}">${esc(shop.name)}${shop.enabled === false || shop.enabled === 0 ? tr("（已停用）", " (disabled)") : ""}</option>`).join("")}`;
        }
        renderRows();
      });
    }
    return instance;
  }
  window.ExtorePipelineAuthorizations = Object.freeze({ mount });
})();
