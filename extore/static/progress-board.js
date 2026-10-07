"use strict";

(() => {
  const mounts = new WeakMap();
  const states = ["queued", "processing", "waiting", "needs_input", "failed", "succeeded", "rejected", "destroyed"];
  const labels = {
    queued: ["排队中", "Queued"], processing: ["处理中", "Processing"], waiting: ["等待顾客", "Waiting for customer"],
    needs_input: ["需要重试", "Retry needed"], failed: ["未完成", "Failed"], succeeded: ["已完成", "Completed"], rejected: ["已拒绝", "Rejected"], destroyed: ["已销毁", "Destroyed"],
  };
  const kinds = { human: ["人工", "Human"], cli: ["CLI", "CLI"], automatic: ["商品处理器", "Product processor"], merchant: ["店主", "Merchant"], unknown: ["处理人员", "Worker"] };
  const phases = { await_start: ["等待开始", "Awaiting start"], input: ["等待输入", "Awaiting input"], display: ["等待继续", "Awaiting continuation"], queued: labels.queued, processing: labels.processing, ended: ["流程结束", "Flow ended"] };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const text = (value, max = 256) => typeof value === "string" && value.length <= max ? value : null;
  const id = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value) ? value : null;
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const time = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 8640000000000;
  const counts = (value) => value && states.every((state) => integer(value[state])) ? Object.fromEntries(states.map((state) => [state, value[state]])) : null;
  // Project the dedicated progress DTO. Never retain arbitrary task fields.
  function project(data, expectedShop, expectedProduct = "") {
    const bad = () => { throw new Error("invalid-progress-board"); };
    if (data?.schema !== "extore.progress-board.v1" || !time(data.generated_at) || id(data.shop?.id) !== expectedShop || text(data.shop?.name) === null) bad();
    const total = counts(data.totals);
    if (!total || !Array.isArray(data.scope?.product_ids) || data.scope.product_ids.length > 500 || data.scope.product_ids.some((value) => !id(value)) || new Set(data.scope.product_ids).size !== data.scope.product_ids.length) bad();
    const scope = new Set(data.scope.product_ids);
    if (expectedProduct && (!scope.has(expectedProduct) || scope.size !== 1)) bad();
    const page = data.pagination;
    if (!page || !integer(page.limit) || page.limit < 1 || page.limit > 200 || !integer(page.offset) || page.offset > 1000000 || !integer(page.total) || typeof page.has_more !== "boolean") bad();
    if (!Array.isArray(data.products) || data.products.length > 500 || !Array.isArray(data.workers) || data.workers.length > 500) bad();
    const jobIds = new Set(), productIds = new Set(), workerIds = new Set();
    const products = data.products.map((p) => {
      if (!id(p.id) || !scope.has(p.id) || productIds.has(p.id) || text(p.name) === null || !counts(p.counts) || !Array.isArray(p.jobs)) bad();
      productIds.add(p.id);
      const jobs = p.jobs.map((j) => {
        if (!id(j.id) || jobIds.has(j.id) || !states.includes(j.state) || typeof j.progress !== "number" || !Number.isFinite(j.progress) || j.progress < 0 || j.progress > 100 || !integer(j.attempt) || !time(j.created) || !time(j.updated) || (j.queue_position !== null && (!integer(j.queue_position) || j.queue_position < 1)) || (j.worker_id !== null && !id(j.worker_id)) || !integer(j.step_count) || !integer(j.completed_step_count) || j.completed_step_count > j.step_count || !Array.isArray(j.steps) || j.steps.length > 256 || (j.flow_phase !== null && !Object.hasOwn(phases, j.flow_phase))) bad();
        jobIds.add(j.id);
        const positions = new Set();
        const steps = j.steps.map((s) => {
          if (!integer(s.position) || s.position < 1 || positions.has(s.position) || !["pending", "current", "done"].includes(s.state)) bad();
          positions.add(s.position);
          return { position: s.position, state: s.state };
        });
        return { id: j.id, state: j.state, progress: j.progress, attempt: j.attempt, created: j.created, updated: j.updated, queue_position: j.queue_position, worker_id: j.worker_id, step_count: j.step_count, completed_step_count: j.completed_step_count, steps, flow_phase: j.flow_phase };
      });
      return { id: p.id, name: p.name, counts: counts(p.counts), jobs };
    });
    if (jobIds.size > page.limit) bad();
    const workers = data.workers.map((w) => {
      if (!id(w.id) || workerIds.has(w.id) || text(w.name) === null || !Object.hasOwn(kinds, w.kind) || !integer(w.active_jobs) || !integer(w.completed_jobs) || (w.last_update !== null && !time(w.last_update))) bad();
      workerIds.add(w.id);
      return { id: w.id, name: w.name, kind: w.kind, active_jobs: w.active_jobs, completed_jobs: w.completed_jobs, last_update: w.last_update };
    });
    if (products.some((p) => p.jobs.some((j) => j.worker_id !== null && !workerIds.has(j.worker_id)))) bad();
    return { generated_at: data.generated_at, shop: { id: data.shop.id, name: data.shop.name }, totals: total, products, workers, pagination: { limit: page.limit, offset: page.offset, total: page.total, has_more: page.has_more } };
  }
  function mount(ctx) {
    const root = ctx.root;
    mounts.get(root)?.dispose();
    const tr = (cn, en) => (typeof ctx.language === "function" ? ctx.language() : ctx.language) === "en" ? en : cn;
    const label = (pair) => tr(...pair);
    const rootScope = ctx.platform === true;
    const allowed = ctx.auth?.role === "admin" || (ctx.auth?.role === "staff" && (ctx.auth.permissions || []).some((p) => ["queue.monitor", "queue.view"].includes(p)));
    const fixedProduct = ctx.auth?.role === "staff" ? ctx.auth.product_id || "" : "";
    let shopId = rootScope ? ctx.shopId || "" : ctx.auth?.shop_id || "", productId = fixedProduct || ctx.productId || "";
    let view = ctx.view === "processed" ? "processed" : "active", offset = 0, paused = false, disposed = false, blocked = false, loading = false;
    let generation = 0, timeout = null, controller = null, snapshot = null, lastSuccess = null, productChoices = [], shops = [], shopsLoaded = !rootScope;
    const $ = (selector) => root.querySelector(selector);
    const current = () => !disposed && root.isConnected !== false && ctx.isCurrent?.() !== false;
    const visible = () => current() && !document.hidden;
    const date = (value) => value === null ? "—" : new Date(value * 1000).toLocaleString(tr("zh-CN", "en"), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const emit = () => ctx.onFilters?.({ shopId, productId, view });
    const cancel = () => { clearTimeout(timeout); timeout = null; generation++; controller?.abort(); controller = null; loading = false; };
    const updateControls = () => {
      if (!current()) return;
      $("#board-refresh").disabled = loading || !shopId || blocked;
      $("#board-pause").disabled = !shopId || blocked;
      $("#board-pause").textContent = paused ? tr("继续自动刷新", "Resume auto-refresh") : tr("暂停自动刷新", "Pause auto-refresh");
      $("#board-copy").disabled = !shopId || blocked;
      $("#board-view").disabled = blocked;
      if (rootScope) $("#board-shop").disabled = blocked;
      $("#board-updated").textContent = lastSuccess === null ? tr("尚未获取进度", "Progress has not been loaded") : tr("最近成功刷新：", "Last successful refresh: ") + date(lastSuccess);
    };
    const setStatus = (cn, en, error = false) => {
      if (!current()) return;
      $("#board-status").textContent = tr(cn, en);
      $("#board-status").setAttribute("role", error ? "alert" : "status");
      updateControls();
    };
    function options() {
      $("#board-product").innerHTML = `${fixedProduct ? "" : `<option value="">${tr("全部商品", "All products")}</option>`}${productChoices.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}`;
      $("#board-product").value = productId;
      $("#board-product").disabled = !!fixedProduct || !shopId || !productChoices.length;
    }
    const elapsed = (job) => {
      const endpoint = ["succeeded", "rejected", "destroyed"].includes(job.state) ? job.updated : snapshot.generated_at;
      const minutes = Math.max(0, Math.floor((endpoint - job.created) / 60));
      return minutes < 60 ? tr(`${minutes} 分钟`, `${minutes} min`) : tr(`${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`, `${Math.floor(minutes / 60)} h ${minutes % 60} min`);
    };
    function renderData() {
      const workers = new Map(snapshot.workers.map((worker) => [worker.id, worker]));
      const shown = snapshot.products.filter((p) => p.jobs.length);
      const page = snapshot.pagination;
      $("#board-data").innerHTML = `<p class="caption">${tr("统计包含当前商品范围的全部状态，不受分页影响。", "Counts include every state in the current product scope, across all pages.")}</p><dl class="board-totals" aria-label="${tr("授权范围内全部任务统计", "All tasks in the authorized scope")}">${states.map((state) => `<div><dt>${esc(label(labels[state]))}</dt><dd>${snapshot.totals[state]}</dd></div>`).join("")}</dl><section class="board-workers"><h3>${tr("处理人员", "Workers")}</h3><p class="caption">${tr("负载只统计正在处理的任务；已处理包括完成、拒绝和销毁。", "Load counts processing tasks only. Processed includes completed, rejected and destroyed tasks.")}</p>${snapshot.workers.length ? `<ul class="board-worker-list">${snapshot.workers.map((w) => `<li><div><strong>${esc(w.name)}</strong><span class="caption">${esc(label(kinds[w.kind]))}</span></div><p>${tr("处理中", "Processing")} <strong>${w.active_jobs}</strong><span>${tr("已处理", "Processed")} ${w.completed_jobs}</span></p><time class="caption">${tr("更新：", "Updated: ")}${esc(date(w.last_update))}</time></li>`).join("")}</ul>` : `<p class="caption">${tr("暂无已领取任务的处理人员。", "No workers have claimed tasks yet.")}</p>`}</section><section class="board-pipelines"><div class="board-section-title"><h3>${view === "active" ? tr("待处理与进行中", "Pending and active") : tr("已处理", "Processed")}</h3><span class="caption">${tr(`共 ${page.total} 个任务`, `${page.total} tasks`)}</span></div>${shown.length ? shown.map((p) => `<section class="board-product"><h4>${esc(p.name)}</h4><ol class="board-job-list">${p.jobs.map((j) => `<li class="board-job"><div class="board-job-heading"><span class="board-job-reference">${tr("任务", "Task")} <span class="mono">${esc(j.id.slice(-8))}</span></span><span class="status ${esc(j.state)}">${esc(label(labels[j.state]))}</span></div><div class="board-job-meta"><span>${j.worker_id ? esc(workers.get(j.worker_id).name) : tr("尚未领取", "Unclaimed")}</span>${j.queue_position !== null ? `<span>${tr(`商品内排第 ${j.queue_position} 位`, `Position ${j.queue_position} in this product`)}</span>` : ""}<span>${tr("第", "Attempt ")}${j.attempt}${tr(" 次尝试", "")}</span>${j.flow_phase ? `<span>${esc(label(phases[j.flow_phase]))}</span>` : ""}</div><div class="board-progress"><progress max="100" value="${j.progress}" aria-label="${tr("上报进度", "Reported progress")}">${j.progress}%</progress><span>${j.progress}%</span></div>${j.step_count ? `<div class="board-step-track" role="list" aria-label="${tr("处理步骤", "Processing steps")}">${j.steps.map((s) => `<span role="listitem" class="board-step ${esc(s.state)}" aria-label="${esc(tr(`步骤 ${s.position}：`, `Step ${s.position}: `) + label({ pending: ["未完成", "Pending"], current: ["当前步骤", "Current step"], done: ["已完成", "Done"] }[s.state]))}">${s.position}</span>`).join("")}</div><p class="caption">${tr(`已完成 ${j.completed_step_count} / ${j.step_count} 步`, `${j.completed_step_count} / ${j.step_count} steps completed`)}</p>` : `<p class="caption">${tr("未配置步骤", "No steps configured")}</p>`}<div class="board-job-footer"><span>${tr("已历时：", "Elapsed: ")}${esc(elapsed(j))}</span><time>${tr("更新：", "Updated: ")}${esc(date(j.updated))}</time></div></li>`).join("")}</ol></section>`).join("") : `<p class="board-empty">${view === "active" ? tr("暂无待处理任务。", "No pending tasks.") : tr("暂无已处理任务。", "No processed tasks.")}</p>`}</section><div class="board-pagination"><button type="button" id="board-prev" class="secondary" ${page.offset === 0 ? "disabled" : ""}>${tr("上一页", "Previous")}</button><span class="caption">${tr(`第 ${Math.floor(page.offset / page.limit) + 1} 页`, `Page ${Math.floor(page.offset / page.limit) + 1}`)}</span><button type="button" id="board-next" class="secondary" ${page.has_more ? "" : "disabled"}>${tr("下一页", "Next")}</button></div>`;
      $("#board-prev").addEventListener("click", () => { if (loading) return; offset = Math.max(0, offset - 100); refresh(); });
      $("#board-next").addEventListener("click", () => { if (loading) return; offset += 100; refresh(); });
    }
    function schedule() {
      clearTimeout(timeout); timeout = null;
      if (visible() && !paused && !blocked && shopId) timeout = setTimeout(() => { if (!current()) { dispose(); return; } refresh(); }, 5000);
    }
    async function refresh() {
      if (!visible() || !shopId || blocked) return;
      cancel();
      const own = generation, expectedShop = shopId, expectedProduct = productId, expectedView = view, expectedOffset = offset;
      controller = new AbortController();
      const signal = controller.signal;
      const active = () => visible() && own === generation && !signal.aborted;
      loading = true;
      setStatus("正在获取真实进度…", "Loading reported progress…");
      try {
        const query = new URLSearchParams({ view, limit: "100", offset: String(offset) });
        if (rootScope) query.set("shop_id", shopId);
        if (productId) query.set("product_id", productId);
        const raw = await ctx.api("/manage/progress-board?" + query, undefined, "GET", { signal });
        if (!active()) return;
        const data = project(raw, expectedShop, expectedProduct);
        if (data.pagination.offset !== expectedOffset || data.pagination.limit !== 100 || data.products.some((p) => p.jobs.some((j) => (expectedView === "active") !== ["queued", "processing", "waiting", "failed", "needs_input"].includes(j.state)))) throw new Error("invalid-progress-board");
        snapshot = data; lastSuccess = data.generated_at;
        if (!expectedProduct || fixedProduct) { productChoices = data.products.map((p) => ({ id: p.id, name: p.name })); options(); }
        if (!rootScope) $("#board-shop-name").textContent = data.shop.name;
        renderData();
        setStatus(paused ? "自动刷新已暂停；显示最近获取的进度。" : "每 5 秒刷新一次，只显示实际进度。", paused ? "Auto-refresh is paused. Showing the last fetched progress." : "Refreshes every 5 seconds. Reported progress only.");
      } catch (error) {
        if (!active() || error?.name === "AbortError") return;
        snapshot = null;
        $("#board-data").innerHTML = "";
        blocked = error?.status === 401 || error?.status === 403;
        if (blocked) { productChoices = []; options(); $("#board-prompt").innerHTML = ""; if (rootScope) { shops = []; $("#board-shop").innerHTML = `<option value="">${tr("请重新登录", "Sign in again")}</option>`; } }
        setStatus(blocked ? "会话或权限已失效，请重新登录后打开看板。" : "连接中断，当前进度不可用。可手动刷新重试。", blocked ? "Your session or permission is no longer valid. Sign in again to open the board." : "Disconnected. Current progress is unavailable. Refresh to retry.", true);
      } finally {
        if (own === generation && current()) { loading = false; controller = null; updateControls(); schedule(); }
      }
    }
    function changed() {
      if (blocked || !current()) return;
      cancel(); snapshot = null; lastSuccess = null; offset = 0; blocked = false;
      $("#board-data").innerHTML = ""; $("#board-prompt").innerHTML = "";
      emit(); options(); updateControls(); refresh();
    }
    function visibility() {
      if (!current()) { dispose(); return; }
      if (document.hidden) { cancel(); updateControls(); }
      else if (!blocked && !shopsLoaded) loadShops();
      else if (!paused && !blocked) refresh();
    }
    function dispose() {
      if (disposed) return;
      cancel(); disposed = true; snapshot = null; shops = []; productChoices = [];
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", dispose);
      if (mounts.get(root) === result) mounts.delete(root);
    }
    const result = { dispose, refresh, filters: () => ({ shopId, productId, view }), ready: null };
    mounts.set(root, result);
    if (!allowed) { root.innerHTML = `<p class="error" role="alert">${tr("没有查看进度看板的权限。", "You do not have permission to view the progress board.")}</p>`; return result; }
    root.innerHTML = `<section class="progress-board"><div class="section-head"><div><h2>${tr("进度看板", "Progress board")}</h2><p class="caption">${tr("只看流水线状态，不显示需求、消息或交付内容。", "Pipeline status only. No requests, messages or delivery content.")}</p></div><div class="actions"><button type="button" id="board-copy" class="secondary">${tr("复制看板 AI 提示词", "Copy board prompt for AI")}</button></div></div><div class="board-toolbar">${rootScope ? `<div class="field"><label for="board-shop">${tr("店铺", "Shop")}</label><select id="board-shop"><option value="">${tr("先选择店铺", "Select a shop first")}</option></select></div>` : `<p id="board-shop-name" class="board-shop-name">${esc(ctx.auth?.shop_name || tr("当前店铺", "Current shop"))}</p>`}<div class="field"><label for="board-product">${tr("商品", "Product")}</label><select id="board-product" disabled></select></div><div class="field"><label for="board-view">${tr("任务范围", "Tasks")}</label><select id="board-view"><option value="active">${tr("待处理与进行中", "Pending and active")}</option><option value="processed">${tr("已处理", "Processed")}</option></select></div><div class="actions"><button type="button" id="board-pause" class="secondary"></button><button type="button" id="board-refresh" class="secondary">${tr("刷新", "Refresh")}</button></div></div><div class="board-connection"><p id="board-status" role="status"></p><p id="board-updated" class="caption"></p></div><div id="board-prompt"></div><div id="board-data" aria-live="off"></div></section>`;
    $("#board-view").value = view;
    $("#board-view").addEventListener("change", () => { view = $("#board-view").value === "processed" ? "processed" : "active"; changed(); });
    $("#board-product").addEventListener("change", () => { const next = $("#board-product").value; if (fixedProduct || (next && !productChoices.some((p) => p.id === next))) return; productId = next; changed(); });
    $("#board-refresh").addEventListener("click", refresh);
    $("#board-pause").addEventListener("click", () => { if (blocked || !current()) return; paused = !paused; if (paused) cancel(); else { clearTimeout(timeout); timeout = null; } setStatus(paused ? "自动刷新已暂停；显示最近获取的进度。" : "每 5 秒刷新一次，只显示实际进度。", paused ? "Auto-refresh is paused. Showing the last fetched progress." : "Refreshes every 5 seconds. Reported progress only."); if (!paused) refresh(); });
    $("#board-copy").addEventListener("click", async () => {
      if (!current() || !shopId || blocked || typeof ctx.copyPrompt !== "function") return;
      const own = generation;
      await ctx.copyPrompt({ boardOnly: true, monitorOnly: true, shopId, productId: productId || null, view, permissions: ["queue.monitor"] }, $("#board-prompt"), () => current() && own === generation);
    });
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", dispose);
    options(); updateControls(); emit();
    async function loadShops() {
      if (!visible() || blocked) return;
      const own = generation;
      controller = new AbortController();
      const signal = controller.signal;
      setStatus("先选择店铺，再查看该店的进度。", "Select a shop to view its progress.");
      try {
        const raw = await ctx.api("/platform/shops", undefined, "GET", { signal });
        if (!current() || own !== generation || signal.aborted) return;
        if (!Array.isArray(raw) || raw.some((shop) => !id(shop.id) || text(shop.name) === null) || new Set(raw.map((shop) => shop.id)).size !== raw.length) throw new Error("invalid-shop-list");
        shops = raw.map((shop) => ({ id: shop.id, name: shop.name }));
        shopsLoaded = true;
        if (shopId && !shops.some((shop) => shop.id === shopId)) { shopId = ""; productId = ""; }
        $("#board-shop").innerHTML = `<option value="">${tr("先选择店铺", "Select a shop first")}</option>` + shops.map((shop) => `<option value="${esc(shop.id)}">${esc(shop.name)}</option>`).join("");
        $("#board-shop").value = shopId;
        $("#board-shop").addEventListener("change", () => { const next = $("#board-shop").value; if (next && !shops.some((shop) => shop.id === next)) return; shopId = next; productId = ""; productChoices = []; changed(); if (!next) setStatus("先选择店铺，再查看该店的进度。", "Select a shop to view its progress."); });
        emit(); updateControls();
        if (shopId) await refresh();
      } catch (error) {
        if (!current() || own !== generation || signal.aborted) return;
        blocked = error?.status === 401 || error?.status === 403;
        setStatus(blocked ? "会话已失效，请重新登录。" : "店铺列表加载失败，请重新打开看板。", blocked ? "Your session is no longer valid. Sign in again." : "Could not load shops. Reopen the board.", true);
      }
    }
    result.ready = rootScope ? loadShops() : shopId && (!fixedProduct || id(fixedProduct)) ? refresh() : Promise.resolve();
    return result;
  }
  window.ExtoreProgressBoard = Object.freeze({ mount });
})();
