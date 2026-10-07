"use strict";

(() => {
  let active = null;
  const labels = {
    unused: "未兑换",
    needs_input: "需要重试",
    queued: "排队中",
    processing: "处理中",
    succeeded: "已完成",
    failed_retryable: "失败，可重试",
    failed_terminal: "失败，不可重试",
    destroyed: "已销毁",
    revoked: "已撤销",
    expired: "已过期",
    rejected: "已拒绝",
  };
  const escape = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const date = (value) =>
    value ? new Date(value * 1000).toLocaleString() : "—";
  const count = (value) => Number(value || 0).toLocaleString();
  const tr = (cn, en) =>
    window.ExtorePreferences?.resolved.language === "en" ? en : cn;
  const normalizeVariants = (variants) =>
    variants.map((variant) => ({
      ...variant,
      id: variant.id || variant.variant_id,
      enabled: variant.enabled !== false,
    }));
  const variantPrice = (variant) => variant.price == null
    ? tr("未设置参考价", "No reference price set")
    : `${tr("参考价", "Reference price")} ${variant.currency || "CNY"} ${variant.price}`;
  const variantLabel = (variant) =>
    `${variant.name}${variant.price != null ? ` · ${variantPrice(variant)}` : ""}${variant.enabled === false ? tr(" · 已停用", " · Disabled") : ""}`;
  const productDeleted = (product) =>
    product?.deleted === true || product?.deleted_at != null || product?.purged === true || product?.purged_at != null;
  const status = (value) => {
    const style = value.startsWith("failed") || value === "rejected"
      ? "failed"
      : ["destroyed", "revoked", "expired"].includes(value)
        ? "destroyed"
        : "";
    return `<span class="status ${style}">${escape(labels[value] || value)}</span>`;
  };

  async function render({
    api,
    workspace,
    products,
    role,
    productId,
    isCurrent = () => true,
    onContextChange = () => {},
  }) {
    active?.dispose();
    const lifetime = new AbortController();
    let read = null;
    let generation = 0;
    let historyGeneration = 0;
    let codeGeneration = 0;
    let busy = false;
    let disposed = false;
    let textImport = null;
    let selected = products.find((product) => product.id === productId)?.id;
    if (!selected)
      selected = products.find((product) => !productDeleted(product))?.id ||
        products[0]?.id || "";
    const selectedProduct = () => products.find((product) => product.id === selected);
    const productVariants = () => {
      const product = selectedProduct();
      return normalizeVariants(
        Array.isArray(product?.variants)
          ? product.variants
          : [
              {
                id: "default",
                name: tr("默认规格", "Default variant"),
                enabled: true,
              },
            ],
      );
    };
    let variants = productVariants();
    let issueVariant = "";
    let inventoryVariant = "";
    let selectedBatch = null;
    let batchView = "active";
    let previewGeneration = 0;
    let offset = 0;
    const limit = 50;
    const endpoint = role === "staff" ? "/manage" : "/admin";
    const current = () =>
      !disposed &&
      active === instance &&
      workspace.isConnected !== false &&
      isCurrent();
    const node = (selector) => workspace.querySelector(selector);
    const instance = {
      get productId() {
        return selected;
      },
      dispose() {
        disposed = true;
        generation++;
        previewGeneration++;
        read?.abort();
        lifetime.abort();
        textImport?.dispose();
      },
    };
    active = instance;
    if (!current()) return instance;
    workspace.innerHTML = `<section class="cards-workspace"><div class="section-head"><h2>${tr("卡密管理", "Redemption codes")}</h2><button id="cards-refresh" class="secondary" ${products.length ? "" : "disabled"}>${tr("刷新", "Refresh")}</button></div>
      <div class="field"><label for="cards-product">选择商品</label><select id="cards-product" ${role === "staff" || !products.length ? "disabled" : ""}>${products.map((product) => `<option value="${escape(product.id)}" ${product.id === selected ? "selected" : ""}>${escape(product.name)}${productDeleted(product) ? tr(" · 已删除", " · Deleted") : ""}</option>`).join("")}</select></div>
      <p id="cards-product-notice" class="caption" role="status" aria-live="polite"></p>
      <div id="cards-error" class="error" role="alert"></div>
      ${
        products.length
          ? `<div id="cards-stats" aria-live="polite"></div>
      <div id="cards-text-import"></div><details id="cards-regular-issue" class="panel"><summary>发行一批卡密</summary><p class="caption">卡密原文只在本次生成时显示。离开页面后不能找回，请立即下载保存。</p>
        <form id="cards-issue"><div class="grid"><div class="field"><label for="cards-issue-variant">${tr("制卡规格", "Variant to issue")}</label><select id="cards-issue-variant" required></select><p id="cards-issue-help" class="caption"></p></div><div class="field"><label for="cards-count">数量</label><input id="cards-count" type="number" min="1" max="1000" step="1" value="10" required></div><div class="field"><label for="cards-label">批次标签（可选）</label><input id="cards-label" maxlength="100" placeholder="例如：十月活动"></div><div class="field"><label for="cards-expires">兑换截止时间（可选，本地时间）</label><input id="cards-expires" type="datetime-local"></div></div><button id="cards-issue-submit" type="submit" class="full">生成卡密</button></form>
      </details><div id="cards-codes" class="secret-output"></div>
      <div class="cards-library"><div class="cards-library-heading"><div><h3 id="cards-library-title">${tr("卡密批次", "Code batches")}</h3><p id="cards-library-note" class="caption">${tr("按发行批次整理，打开文件夹查看卡密。", "Organized by issue batch. Open a folder to view its codes.")}</p></div><div class="actions"><button id="cards-back" class="secondary" hidden>${tr("返回批次", "Back to batches")}</button><button id="cards-folder-action" class="danger" hidden>${tr("删除批次", "Delete batch")}</button></div></div><form id="cards-filter"><div class="cards-filter-grid"><div class="field"><label for="cards-view">${tr("批次视图", "Batch view")}</label><select id="cards-view"><option value="active">${tr("有效批次", "Active batches")}</option><option value="deleted">${tr("回收站", "Trash")}</option></select></div><div class="field"><label for="cards-variant">${tr("查看规格", "Filter by variant")}</label><select id="cards-variant"></select></div><div id="cards-status-field" class="field" hidden><label for="cards-status">${tr("状态", "Status")}</label><select id="cards-status"><option value="">${tr("全部状态", "All statuses")}</option>${Object.entries(
        labels,
      )
        .map(([value, label]) => `<option value="${value}">${label}</option>`)
        .join(
          "",
        )}</select></div><div class="field cards-search-field"><label id="cards-search-label" for="cards-search">${tr("批次标签、卡密 ID 或尾号", "Batch label, code ID or suffix")}</label><input id="cards-search" maxlength="128" autocomplete="off" placeholder="${tr("搜索此商品的批次", "Search this product's batches")}"></div></div><div class="actions"><button type="submit" class="secondary">${tr("筛选", "Filter")}</button><button id="cards-reset" type="button" class="secondary">${tr("清空筛选", "Clear filters")}</button></div></form>
      <div id="cards-batch-review" class="cards-batch-review" role="region" aria-label="${tr("批次操作确认", "Review batch action")}" hidden></div><div id="cards-inventory" aria-live="polite"></div><div class="toolbar"><button id="cards-previous" class="secondary" disabled>${tr("上一页", "Previous")}</button><span id="cards-page" class="caption"></span><button id="cards-next" class="secondary" disabled>${tr("下一页", "Next")}</button></div></div><div id="cards-history"></div>`
          : '<div class="empty">先创建商品，再发行卡密。</div>'
      }</section>`;
    if (!products.length) return instance;
    onContextChange(selected);

    const clearError = () => {
      if (current()) node("#cards-error").textContent = "";
    };
    const setBusy = (value) => {
      busy = value;
      workspace.querySelectorAll("input, select, button").forEach((element) => {
        if (value) {
          if (element.dataset.cardsDisabled === undefined)
            element.dataset.cardsDisabled = String(element.disabled);
          element.disabled = true;
        } else if (element.dataset.cardsDisabled !== undefined) {
          element.disabled = element.dataset.cardsDisabled === "true";
          delete element.dataset.cardsDisabled;
        }
      });
    };
    const disable = (element, value) => {
      if (busy) {
        element.dataset.cardsDisabled = String(value);
        element.disabled = true;
      } else element.disabled = value;
    };
    const clearCodes = () => {
      codeGeneration++;
      node("#cards-codes").innerHTML = "";
    };
    const variantControls = () => {
      const deleted = productDeleted(selectedProduct());
      const enabled = variants.filter((variant) => variant.enabled);
      if (!enabled.some((variant) => variant.id === issueVariant))
        issueVariant =
          enabled.find((variant) => variant.id === "default")?.id ||
          enabled[0]?.id ||
          "";
      const issueSelect = node("#cards-issue-variant");
      issueSelect.innerHTML = enabled.length
        ? enabled
            .map(
              (variant) =>
                `<option value="${escape(variant.id)}">${escape(variantLabel(variant))}</option>`,
            )
            .join("")
        : `<option value="">${tr("暂无启用规格", "No enabled variants")}</option>`;
      issueSelect.value = issueVariant;
      const canIssue = !deleted && enabled.length > 0;
      disable(issueSelect, !canIssue);
      disable(node("#cards-issue-submit"), !canIssue);
      for (const selector of ["#cards-count", "#cards-label", "#cards-expires"])
        disable(node(selector), !canIssue);
      node("#cards-product-notice").textContent = deleted
        ? tr("此商品已删除。已有卡密、统计与使用记录仍可查看，不能再发行卡密或补充库存。", "This product was deleted. Existing codes, statistics and usage history remain available. Issuing codes and adding stock are disabled.")
        : "";
      node("#cards-issue-help").textContent = deleted
        ? tr("已删除商品不能生成卡密。", "Deleted products cannot issue codes.")
        : enabled.length
        ? tr(
            "每张卡密绑定选定规格。顾客兑换时无需再选择规格。",
            "Each code is bound to this variant. Customers do not choose a variant when redeeming.",
          )
        : tr(
            "此商品暂无启用规格，无法生成卡密。请商品管理者启用规格；已有卡密仍可查询。",
            "No enabled variants. A product manager must enable one before issuing codes. Existing codes remain searchable.",
          );
      if (!variants.some((variant) => variant.id === inventoryVariant))
        inventoryVariant = "";
      const inventorySelect = node("#cards-variant");
      inventorySelect.innerHTML =
        `<option value="">${tr("所有规格", "All variants")}</option>` +
        variants
          .map(
            (variant) =>
              `<option value="${escape(variant.id)}">${escape(variantLabel(variant))}</option>`,
          )
          .join("");
      inventorySelect.value = inventoryVariant;
    };
    variantControls();
    const drawTextImport = () => {
      textImport?.dispose();
      textImport = null;
      node("#cards-text-import").innerHTML = "";
      const product = selectedProduct();
      const stock = product?.mode === "stock";
      node("#cards-regular-issue").hidden = stock;
      if (!stock || productDeleted(product)) return;
      if (!window.ExtoreTextCards) {
        node("#cards-text-import").textContent = tr("文本导入未加载，请刷新页面。", "Text import did not load. Refresh this page.");
        return;
      }
      textImport = window.ExtoreTextCards.mount({
        root: node("#cards-text-import"), api, endpoint, product, variants,
        isCurrent: current, onBusy: setBusy, onIssued: () => {
          selectedBatch = null;
          batchView = "active";
          node("#cards-view").value = batchView;
          offset = 0;
          return load();
        },
      });
    };
    const perform = async (callback, mutation = false) => {
      if (!current() || busy) return;
      clearError();
      if (mutation) setBusy(true);
      try {
        await callback();
      } catch (error) {
        if (current() && error.name !== "AbortError")
          node("#cards-error").textContent =
            error.message || "操作失败，请重试";
      } finally {
        if (mutation && current()) setBusy(false);
      }
    };
    const listen = (selector, event, callback, mutation = false) => {
      node(selector)?.addEventListener(event, (eventObject) => {
        if (event === "submit") eventObject.preventDefault();
        void perform(() => callback(eventObject), mutation);
      });
    };
    const query = () => {
      const params = new URLSearchParams({
        product_id: selected,
        offset: String(offset),
        limit: String(limit),
      });
      for (const [key, selector] of [
        ["variant_id", "#cards-variant"],
        ["search", "#cards-search"],
      ]) {
        const value = node(selector).value.trim();
        if (value) params.set(key, value);
      }
      if (selectedBatch) {
        params.set("batch_id", selectedBatch.id);
        const state = node("#cards-status").value;
        if (state) params.set("status", state);
      } else params.set("view", batchView);
      return params;
    };
    const overview = (stats) => {
      const summary = stats.summary || {};
      const metrics = [
        ["总发行", "total"],
        ["剩余未兑换", "remaining"],
        ["已使用", "used"],
        ["正在处理", "in_progress"],
      ];
      node("#cards-stats").innerHTML =
        `<div class="cards-summary">${metrics.map(([label, key]) => `<div class="cards-metric"><p class="caption">${label}</p><h2>${count(summary[key])}</h2></div>`).join("")}</div>
        <p class="caption">剩余未兑换包含未提交及需要重试的有效卡密（${count(summary.states?.needs_input)} 张），已退回的卡密不计入已使用。处理失败、可重试的卡密 ${count(summary.states?.failed_retryable)} 张另行统计，不计入未兑换数量。</p>
        <p class="caption">${tr("商品统计包含已归档的任务卡密；下方文件夹只列当前视图。", "Product totals include archived task codes; folders below show the current view.")}</p>
        <p class="caption">已验码 ${count(summary.verified)} · 已领取 ${count(summary.viewed)} · 已完成 ${count(summary.completed)} · 失败 ${count(summary.failed)} · 已拒绝 ${count(summary.rejected)} · 已撤销 ${count(summary.states?.revoked)} · 已过期 ${count(summary.states?.expired)}</p>`;
      if (Array.isArray(stats.variants) && stats.variants.length) {
        const total = stats.variants.reduce(
          (sum, variant) => sum + Number(variant.summary?.total || 0),
          0,
        );
        const remaining = stats.variants.reduce(
          (sum, variant) => sum + Number(variant.summary?.remaining || 0),
          0,
        );
        node("#cards-stats").innerHTML +=
          `<details class="card-variant-summary"><summary>${tr("各规格卡密统计", "Codes by variant")}</summary><div class="card-variant-grid">${stats.variants.map((variant) => `<article class="card-variant-metric"><h3>${escape(variant.name)}</h3><p class="caption">${escape(variantPrice(variant))}${variant.enabled === false ? tr(" · 已停用", " · Disabled") : ""}</p><dl class="card-variant-counts"><div><dt>${tr("总发行", "Issued")}</dt><dd>${count(variant.summary?.total)}</dd></div><div><dt>${tr("未兑换卡密", "Unredeemed")}</dt><dd>${count(variant.summary?.remaining)}</dd></div></dl></article>`).join("")}</div><p class="caption">${tr(`规格合计：总发行 ${count(total)} 张，未兑换 ${count(remaining)} 张；均计入上方商品总量。`, `Variant totals: ${count(total)} issued, ${count(remaining)} unredeemed; included in the product totals above.`)}</p></details>`;
      }
    };
    const showInventory = (inventory) => {
      const items = Array.isArray(inventory.items) ? inventory.items : [];
      const total = Number(inventory.total || 0);
      node("#cards-inventory").innerHTML = items.length
        ? `<div class="table-wrap"><table class="card-inventory-table"><thead><tr><th>卡密 / 尾号</th><th>批次</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>${items.map((item) => `<tr><td class="mono" data-label="卡密 / 尾号">${escape(item.id)}<div class="caption">${item.code_suffix ? "····" + escape(item.code_suffix) : "旧卡密无尾号记录"}</div><div class="caption">${tr("规格", "Variant")}：${escape(item.variant_name || tr("默认规格", "Default variant"))}</div></td><td data-label="批次">${escape(item.batch_label || "—")}<div class="mono muted">${escape(item.batch_id || "")}</div></td><td data-label="状态">${status(String(item.status || ""))}${item.attempt ? `<p class="caption">第 ${count(item.attempt)} 次尝试</p>` : ""}</td><td data-label="时间"><div>发行 ${escape(date(item.created))}</div>${item.used_at ? `<div>首次提交 ${escape(date(item.used_at))}</div>` : ""}${item.expires ? `<div class="caption">截止 ${escape(date(item.expires))}</div>` : '<div class="caption">无兑换截止时间</div>'}</td><td data-label="操作"><div class="row-tools"><button class="secondary" data-card-history="${escape(item.id)}">使用记录</button>${["unused", "needs_input"].includes(item.status) ? `<button class="danger" data-card-revoke="${escape(item.id)}">撤销</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>`
        : '<div class="empty">没有符合条件的卡密。</div>';
      node("#cards-page").textContent = total
        ? `${offset + 1}–${Math.min(offset + items.length, total)} / ${count(total)} 张`
        : "0 张";
      disable(node("#cards-previous"), offset === 0);
      disable(node("#cards-next"), offset + limit >= total);
      node("#cards-inventory")
        .querySelectorAll("[data-card-history]")
        .forEach((button) =>
          button.addEventListener(
            "click",
            () => void perform(() => history(button.dataset.cardHistory)),
          ),
        );
      node("#cards-inventory")
        .querySelectorAll("[data-card-revoke]")
        .forEach((button) =>
          button.addEventListener(
            "click",
            () =>
              void perform(async () => {
                if (!window.confirm("撤销后，此卡密将无法兑换。确认撤销？"))
                  return;
                await api(
                  `${endpoint}/cards/${encodeURIComponent(button.dataset.cardRevoke)}/revoke?product_id=${encodeURIComponent(selected)}`,
                  {},
                  "POST",
                  { signal: lifetime.signal },
                );
                if (!current()) return;
                node("#cards-history").innerHTML = "";
                await load();
              }, true),
          ),
        );
      if (busy) setBusy(true);
    };
    const clearReview = () => {
      previewGeneration++;
      node("#cards-batch-review").innerHTML = "";
      node("#cards-batch-review").hidden = true;
    };
    const batchUrl = (id, suffix = "") =>
      `${endpoint}/card-batches/${encodeURIComponent(id)}${suffix}?product_id=${encodeURIComponent(selected)}`;
    const updateLibrary = () => {
      const inside = Boolean(selectedBatch);
      node("#cards-back").hidden = !inside;
      node("#cards-folder-action").hidden = !inside;
      node("#cards-status-field").hidden = !inside;
      node("#cards-library-title").textContent = inside
        ? selectedBatch.label || tr("未命名批次", "Untitled batch")
        : batchView === "deleted" ? tr("批次回收站", "Batch trash") : tr("卡密批次", "Code batches");
      node("#cards-library-note").textContent = inside
        ? tr("此文件夹只显示卡密使用情况，不包含原文或交付内容。", "This folder shows code usage, without original codes or delivery content.")
        : batchView === "deleted"
          ? tr("已删除批次可恢复，或预览后永久清理。", "Restore deleted batches, or review them before permanent cleanup.")
          : tr("按发行批次整理，打开文件夹查看卡密。", "Organized by issue batch. Open a folder to view its codes.");
      node("#cards-folder-action").textContent = batchView === "deleted"
        ? tr("永久清理批次", "Permanently clean batch") : tr("删除批次", "Delete batch");
      node("#cards-search-label").textContent = inside
        ? tr("卡密 ID 或尾号", "Code ID or suffix") : tr("批次标签、卡密 ID 或尾号", "Batch label, code ID or suffix");
      node("#cards-search").placeholder = inside
        ? tr("不需要输入完整卡密", "No complete code needed") : tr("搜索此商品的批次", "Search this product's batches");
    };
    const openBatch = async (batch) => {
      selectedBatch = { ...batch, label: batch.label || (batch.legacy ? tr("历史卡密", "Legacy codes") : tr("未命名批次", "Untitled batch")) };
      offset = 0;
      node("#cards-search").value = "";
      node("#cards-status").value = "";
      node("#cards-history").innerHTML = "";
      updateLibrary();
      await load();
    };
    const restoreBatch = async (batch) => {
      await api(batchUrl(batch.id, "/restore"), {}, "POST", { signal: lifetime.signal });
      if (!current()) return;
      selectedBatch = null;
      node("#cards-history").innerHTML = "";
      offset = 0;
      await load();
    };
    const reviewBatch = async (batch) => {
      clearReview();
      const scope = selected;
      const view = batchView;
      const requestGeneration = generation;
      const reviewGeneration = previewGeneration;
      const purge = view === "deleted";
      const preview = await api(batchUrl(batch.id, purge ? "/purge-preview" : "/delete-preview"), {}, "POST", { signal: lifetime.signal });
      if (!current() || scope !== selected || view !== batchView || requestGeneration !== generation || reviewGeneration !== previewGeneration) return;
      if (typeof preview.revision !== "string" || !preview.revision)
        throw new Error(tr("没有收到批次修订，请刷新后重试。", "No batch revision received. Refresh and try again."));
      const label = preview.batch?.label || batch.label || tr("未命名批次", "Untitled batch");
      const review = node("#cards-batch-review");
      review.hidden = false;
      review.innerHTML = `<h4>${purge ? tr("永久清理", "Permanent cleanup") : tr("移入回收站", "Move to trash")} · ${escape(label)}</h4><dl class="cards-review-counts">${purge
        ? `<div><dt>${tr("永久删除", "Permanently delete")}</dt><dd>${count(preview.delete_count)}</dd></div>`
        : `<div><dt>${tr("停止兑换", "Stop redemption")}</dt><dd>${count(preview.revocable_count)}</dd></div>`}<div><dt>${tr("保留记录", "Retain records")}</dt><dd>${count(preview.retain_count)}</dd></div>${!purge ? `<div><dt>${tr("正在处理", "In progress")}</dt><dd>${count(preview.in_progress)}</dd></div>` : ""}</dl><p>${escape(preview.explanation || (purge
        ? tr("清理没有任务记录的卡密。已有任务与交付继续保留，此操作无法撤回。", "Remove codes without task history. Existing tasks and deliveries stay available. This cannot be undone.")
        : tr("批次从有效列表移除。未开始或可重试卡密停止兑换，正在处理的任务与交付保留，可在回收站恢复批次。", "Remove the batch from the active list and stop unused or retryable codes. Keep running tasks and deliveries. Restore the batch from trash.")))}</p><div class="actions"><button id="cards-batch-confirm" class="danger">${purge ? tr("确认永久清理", "Confirm permanent cleanup") : tr("确认删除批次", "Confirm batch deletion")}</button><button id="cards-batch-cancel" class="secondary">${tr("取消", "Cancel")}</button></div>`;
      listen("#cards-batch-cancel", "click", clearReview);
      listen("#cards-batch-confirm", "click", async () => {
        if (scope !== selected || view !== batchView || requestGeneration !== generation || reviewGeneration !== previewGeneration) return;
        clearReview();
        await api(batchUrl(batch.id, purge ? "/purge" : ""), { revision: preview.revision, confirmed: true }, purge ? "POST" : "DELETE", { signal: lifetime.signal });
        if (!current() || scope !== selected) return;
        selectedBatch = null;
        clearCodes();
        node("#cards-history").innerHTML = "";
        offset = 0;
        await load();
      }, true);
      review.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      if (busy) setBusy(true);
    };
    const showBatches = (result) => {
      const batches = (Array.isArray(result.items) ? result.items : []).filter((batch) => typeof batch.id === "string" && batch.id);
      const total = Number(result.total || 0);
      const folder = '<svg class="cards-folder-icon" viewBox="0 0 40 32" aria-hidden="true" focusable="false"><path d="M2 7V4h13l5 5h18v21H2V7Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M2 12h36" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';
      node("#cards-inventory").innerHTML = batches.length
        ? `<ul class="cards-folder-list">${batches.map((batch) => `<li class="cards-folder-row"><button class="cards-folder-open" data-card-batch="${escape(batch.id)}">${folder}<span class="cards-folder-copy"><strong>${escape(batch.label || (batch.legacy ? tr("历史卡密", "Legacy codes") : tr("未命名批次", "Untitled batch")))}</strong><span class="cards-folder-counts">${tr("总数", "Total")} ${count(batch.total)} <span>·</span> ${tr("未兑换", "Unredeemed")} ${count(batch.remaining)} <span>·</span> ${tr("已使用", "Used")} ${count(batch.used)}</span><span class="cards-folder-meta">${escape(date(batch.created))}${batch.variant_id ? ` · ${escape(variants.find((variant) => variant.id === batch.variant_id)?.name || batch.variant_id)}` : ""}${Number(batch.in_progress) ? ` · ${tr("处理中", "Processing")} ${count(batch.in_progress)}` : ""}</span></span><span class="cards-folder-arrow" aria-hidden="true">→</span></button><div class="cards-folder-tools">${batchView === "deleted" ? `<button class="secondary" data-card-batch-restore="${escape(batch.id)}">${tr("恢复", "Restore")}</button>` : ""}<button class="cards-folder-remove" data-card-batch-review="${escape(batch.id)}">${batchView === "deleted" ? tr("永久清理", "Permanent cleanup") : tr("删除批次", "Delete batch")}</button></div></li>`).join("")}</ul>`
        : `<div class="cards-folder-empty"><h4>${batchView === "deleted" ? tr("回收站为空", "Trash is empty") : tr("暂无符合条件的批次", "No matching batches")}</h4><p class="caption">${batchView === "deleted" ? tr("删除的批次会放在这里。", "Deleted batches appear here.") : tr("发行卡密或导入文本后，会自动整理成批次文件夹。", "Issuing codes or importing text creates a batch folder.")}</p></div>`;
      node("#cards-page").textContent = total
        ? `${offset + 1}–${Math.min(offset + batches.length, total)} / ${count(total)} ${tr("个批次", "batches")}` : tr("0 个批次", "0 batches");
      disable(node("#cards-previous"), offset === 0);
      disable(node("#cards-next"), offset + limit >= total);
      for (const [attribute, callback, mutation] of [
        ["cardBatch", openBatch, false], ["cardBatchReview", reviewBatch, true], ["cardBatchRestore", restoreBatch, true],
      ]) {
        const selector = attribute.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
        node("#cards-inventory").querySelectorAll(`[data-${selector}]`).forEach((button) => {
          const batch = batches.find((item) => item.id === button.dataset[attribute]);
          button.addEventListener("click", () => void perform(() => callback(batch), mutation));
        });
      }
      if (busy) setBusy(true);
    };
    const load = async () => {
      clearReview();
      updateLibrary();
      const requestGeneration = ++generation;
      read?.abort();
      read = new AbortController();
      const signal = read.signal;
      const scope = encodeURIComponent(selected);
      let stats, inventory;
      try {
        [stats, inventory] = await Promise.all([
          api(`${endpoint}/card-stats?product_id=${scope}`, undefined, "GET", {
            signal,
          }),
          api(`${endpoint}/${selectedBatch ? "card-inventory" : "card-batches"}?${query()}`, undefined, "GET", {
            signal,
          }),
        ]);
      } catch (error) {
        if (!current() || requestGeneration !== generation) return;
        throw error;
      }
      if (!current() || requestGeneration !== generation) return;
      if (offset && Number(inventory.total || 0) <= offset) {
        offset =
          Math.max(0, Math.ceil(Number(inventory.total || 0) / limit) - 1) *
          limit;
        await load();
        return;
      }
      if (Array.isArray(stats.variants)) {
        variants = normalizeVariants(stats.variants);
        variantControls();
      }
      overview(stats);
      if (selectedBatch) showInventory(inventory);
      else showBatches(inventory);
    };
    const history = async (id) => {
      const requestGeneration = generation;
      const requestHistoryGeneration = ++historyGeneration;
      const scope = selected;
      let details;
      try {
        details = await api(
          `${endpoint}/cards/${encodeURIComponent(id)}/history?product_id=${encodeURIComponent(scope)}`,
          undefined,
          "GET",
          { signal: read?.signal || lifetime.signal },
        );
      } catch (error) {
        if (
          !current() ||
          requestGeneration !== generation ||
          requestHistoryGeneration !== historyGeneration
        )
          return;
        throw error;
      }
      if (
        !current() ||
        requestGeneration !== generation ||
        requestHistoryGeneration !== historyGeneration ||
        scope !== selected
      )
        return;
      const card = details.card || {};
      const timeline = Array.isArray(details.timeline) ? details.timeline : [];
      node("#cards-history").innerHTML =
        `<section class="panel"><div class="section-head"><h3>使用记录</h3><button id="cards-history-close" class="secondary">收起</button></div><p class="mono">${escape(card.id || id)} ${card.code_suffix ? "· 尾号 " + escape(card.code_suffix) : ""}</p><p class="caption">${escape(card.product_name || "")} · ${escape(card.variant_name || tr("默认规格", "Default variant"))} · ${escape(card.batch_label || "无批次标签")}</p><p>${status(String(card.status || ""))}</p>${timeline.length ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>处理情况</th></tr></thead><tbody>${timeline.map((event) => `<tr><td>${escape(date(event.created))}</td><td>${escape(eventLabel(event.type))}</td><td>${event.state ? escape(labels[event.state] || event.state) : "—"}${event.attempt ? ` · 第 ${count(event.attempt)} 次尝试` : ""}${event.progress !== undefined ? ` · ${count(event.progress)}%` : ""}</td></tr>`).join("")}</tbody></table></div>` : '<p class="caption">暂无使用记录。</p>'}<p class="caption">这里不显示卡密原文、顾客填写的信息或交付内容。</p></section>`;
      listen("#cards-history-close", "click", () => {
        historyGeneration++;
        node("#cards-history").innerHTML = "";
      });
      node("#cards-history").scrollIntoView?.({
        behavior: "smooth",
        block: "nearest",
      });
    };
    listen("#cards-product", "change", async () => {
      const nextProduct = node("#cards-product").value;
      if (!products.some((product) => product.id === nextProduct)) return;
      selected = nextProduct;
      clearCodes();
      variants = productVariants();
      issueVariant = "";
      inventoryVariant = "";
      selectedBatch = null;
      batchView = "active";
      node("#cards-view").value = batchView;
      variantControls();
      drawTextImport();
      offset = 0;
      node("#cards-status").value = "";
      node("#cards-search").value = "";
      node("#cards-history").innerHTML = "";
      node("#cards-stats").innerHTML = "";
      node("#cards-inventory").innerHTML =
        `<div class="loading">${tr("正在读取这个商品的批次…", "Loading this product's batches…")}</div>`;
      node("#cards-page").textContent = "";
      disable(node("#cards-previous"), true);
      disable(node("#cards-next"), true);
      onContextChange(selected);
      await load();
    });
    listen("#cards-refresh", "click", load);
    listen("#cards-back", "click", async () => {
      selectedBatch = null;
      offset = 0;
      node("#cards-search").value = "";
      node("#cards-status").value = "";
      node("#cards-history").innerHTML = "";
      await load();
    });
    listen("#cards-view", "change", async () => {
      const value = node("#cards-view").value;
      if (!["active", "deleted"].includes(value)) return;
      batchView = value;
      selectedBatch = null;
      offset = 0;
      node("#cards-search").value = "";
      node("#cards-status").value = "";
      node("#cards-history").innerHTML = "";
      await load();
    });
    listen("#cards-folder-action", "click", () => selectedBatch && reviewBatch(selectedBatch), true);
    listen("#cards-issue-variant", "change", () => {
      const value = node("#cards-issue-variant").value;
      if (
        value === issueVariant ||
        !variants.some((variant) => variant.id === value && variant.enabled)
      )
        return;
      issueVariant = value;
      clearCodes();
    });
    for (const selector of ["#cards-count", "#cards-label", "#cards-expires"]) {
      listen(selector, "input", clearCodes);
      listen(selector, "change", clearCodes);
    }
    listen("#cards-variant", "change", async () => {
      const value = node("#cards-variant").value;
      if (value && !variants.some((variant) => variant.id === value)) return;
      inventoryVariant = value;
      offset = 0;
      node("#cards-history").innerHTML = "";
      await load();
    });
    listen("#cards-filter", "submit", async () => {
      inventoryVariant = node("#cards-variant").value;
      offset = 0;
      node("#cards-history").innerHTML = "";
      await load();
    });
    listen("#cards-reset", "click", async () => {
      inventoryVariant = "";
      for (const selector of [
        "#cards-variant",
        "#cards-status",
        "#cards-search",
      ])
        node(selector).value = "";
      offset = 0;
      node("#cards-history").innerHTML = "";
      await load();
    });
    listen("#cards-previous", "click", async () => {
      offset = Math.max(0, offset - limit);
      await load();
    });
    listen("#cards-next", "click", async () => {
      offset += limit;
      await load();
    });
    listen(
      "#cards-issue",
      "submit",
      async () => {
        if (productDeleted(selectedProduct()))
          throw new Error(tr("已删除商品不能生成卡密。", "Deleted products cannot issue codes."));
        const requestedVariant = node("#cards-issue-variant").value;
        const variant = variants.find(
          (variant) => variant.id === requestedVariant && variant.enabled,
        );
        if (!variant)
          throw new Error(
            tr("请选择启用的制卡规格", "Choose an enabled variant"),
          );
        issueVariant = requestedVariant;
        const input = node("#cards-expires").value;
        const expires = input
          ? Math.floor(new Date(input).getTime() / 1000)
          : null;
        if (
          input &&
          (!Number.isFinite(expires) || expires <= Date.now() / 1000)
        )
          throw new Error("兑换截止时间必须晚于当前时间");
        const requestedProduct = selected;
        const result = await api(
          `${endpoint}/cards`,
          {
            product_id: requestedProduct,
            variant_id: requestedVariant,
            count: Number(node("#cards-count").value),
            label: node("#cards-label").value.trim(),
            expires,
          },
          "POST",
          { signal: lifetime.signal },
        );
        if (
          !current() ||
          requestedProduct !== selected ||
          requestedVariant !== issueVariant
        )
          return;
        const codes = Array.isArray(result.codes) ? result.codes : [];
        const issuedGeneration = ++codeGeneration;
        const codesCurrent = () =>
          current() &&
          issuedGeneration === codeGeneration &&
          requestedProduct === selected;
        node("#cards-codes").innerHTML =
          `<label for="cards-generated">${escape(variant.name)} · ${tr(`本次生成 ${count(codes.length)} 张卡密，请立即保存`, `${count(codes.length)} codes generated. Save them now.`)}</label><textarea id="cards-generated" readonly spellcheck="false"></textarea><div class="actions"><button id="cards-copy-all" type="button" class="secondary">${tr("复制全部", "Copy all")}</button><button id="cards-download" class="secondary">${tr("下载文本", "Download text")}</button>${result.batch_id ? `<button id="cards-issued-batch" class="secondary">${tr("查看本次批次", "View this batch")}</button>` : ""}</div><details><summary>${tr("逐条复制", "Copy individual codes")}</summary><div class="field"><label for="cards-single">${tr("选择一条卡密", "Choose a code")}</label><select id="cards-single">${codes.map((code, index) => `<option value="${index}">${tr(`第 ${index + 1} 条`, `Code ${index + 1}`)} · ····${escape(String(code).slice(-6))}</option>`).join("")}</select></div><div class="actions"><button id="cards-copy-single" type="button" class="secondary">${tr("复制这一条", "Copy this code")}</button></div></details><p id="cards-copy-feedback" class="caption" role="status" aria-live="polite"></p>`;
        node("#cards-generated").value = codes.join("\n");
        let copying = false;
        const copy = async (index = null) => {
          if (!codesCurrent() || copying || !codes.length) return;
          if (
            index !== null &&
            (!Number.isInteger(index) || index < 0 || index >= codes.length)
          )
            throw new Error(tr("请选择一条卡密", "Choose a code"));
          copying = true;
          const buttons = [node("#cards-copy-all"), node("#cards-copy-single")];
          buttons.forEach((button) => disable(button, true));
          node("#cards-copy-feedback").textContent = "";
          try {
            const value = index === null ? codes.join("\n") : codes[index];
            const clipboard = window.ExtoreClipboard;
            if (typeof clipboard?.writeText === "function") {
              if (!(await clipboard.writeText(value)))
                throw new Error("Clipboard unavailable");
            } else {
              if (!globalThis.navigator?.clipboard?.writeText)
                throw new Error("Clipboard unavailable");
              await navigator.clipboard.writeText(value);
            }
            if (codesCurrent())
              node("#cards-copy-feedback").textContent =
                index === null
                  ? tr(
                      `已复制 ${count(codes.length)} 条卡密。`,
                      `Copied ${count(codes.length)} ${codes.length === 1 ? "code" : "codes"}.`,
                    )
                  : tr(
                      `已复制第 ${index + 1} 条卡密。`,
                      `Copied code ${index + 1}.`,
                    );
          } catch {
            if (!codesCurrent()) return;
            const generated = node("#cards-generated");
            generated.focus?.();
            if (index === null) generated.select?.();
            else {
              const start =
                codes.slice(0, index).join("\n").length + (index ? 1 : 0);
              generated.setSelectionRange?.(start, start + codes[index].length);
            }
            node("#cards-copy-feedback").textContent = tr(
              "复制失败。请手动复制已选中的卡密，或下载文本。",
              "Copy failed. Copy the selected code text manually, or download it.",
            );
          } finally {
            copying = false;
            if (codesCurrent())
              buttons.forEach((button) => disable(button, false));
          }
        };
        listen("#cards-copy-all", "click", () => copy());
        listen("#cards-copy-single", "click", () =>
          copy(Number(node("#cards-single").value)),
        );
        listen("#cards-download", "click", () => {
          if (!codesCurrent()) return;
          const url = URL.createObjectURL(
            new Blob([codes.join("\n")], { type: "text/plain;charset=utf-8" }),
          );
          const anchor = document.createElement("a");
          anchor.href = url;
          const filename = `${Array.from(variant.name).slice(0, 32).join("")}-${requestedVariant}`.replace(
            /[\\/:*?"<>|\u0000-\u001f\u007f]/g,
            "_",
          );
          anchor.download = `extore-codes-${filename}-${result.batch_id || Date.now()}.txt`;
          anchor.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
        if (result.batch_id)
          listen("#cards-issued-batch", "click", async () => {
            if (!codesCurrent()) return;
            selectedBatch = { id: result.batch_id, label: node("#cards-label").value.trim() || tr("本次发行", "Newly issued batch") };
            batchView = "active";
            node("#cards-view").value = batchView;
            inventoryVariant = requestedVariant;
            node("#cards-variant").value = requestedVariant;
            node("#cards-status").value = "";
            node("#cards-search").value = "";
            offset = 0;
            await load();
          });
        await load();
      },
      true,
    );
    drawTextImport();
    await perform(load);
    return instance;
  }

  function eventLabel(type) {
    return (
      {
        "card.issued": "发行卡密",
        "card.verified": "首次验码",
        "card.revoked": "撤销卡密",
        "redemption.requested": "提交兑换",
        "redemption.processing": "开始处理",
        "redemption.progress": "更新进度",
        "redemption.succeeded": "兑换完成",
        "redemption.failed": "兑换失败",
        "redemption.retried": "再次尝试",
        "fulfillment.progress": "更新处理进度",
        "fulfillment.succeeded": "兑换完成",
        "fulfillment.failed": "兑换失败",
        "fulfillment.needs_input": "需要重试",
        "fulfillment.rejected": "拒绝兑换",
        "delivery.viewed": "领取内容",
        "delivery.revealed": "领取内容",
        "delivery.destroyed": "销毁内容",
      }[type] || type
    );
  }

  window.ExtoreCards = Object.freeze({ render });
})();
