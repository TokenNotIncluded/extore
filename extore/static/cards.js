"use strict";

(() => {
  let active = null;
  const labels = {
    unused: "未兑换",
    queued: "排队中",
    processing: "处理中",
    succeeded: "已完成",
    failed_retryable: "失败，可重试",
    failed_terminal: "失败，不可重试",
    destroyed: "已销毁",
    revoked: "已撤销",
    expired: "已过期",
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
  const variantLabel = (variant) =>
    `${variant.name}${variant.price != null ? ` · ${variant.currency || "CNY"} ${variant.price}` : ""}${variant.enabled === false ? tr(" · 已停用", " · Disabled") : ""}`;
  const status = (value) => {
    const style = value.startsWith("failed")
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
    let selected = products.find((product) => product.id === productId)?.id;
    if (!selected) selected = products[0]?.id || "";
    const productVariants = () => {
      const product = products.find((product) => product.id === selected);
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
        read?.abort();
        lifetime.abort();
      },
    };
    active = instance;
    if (!current()) return instance;
    workspace.innerHTML = `<div class="section-head"><h2>卡密管理</h2><button id="cards-refresh" class="secondary" ${products.length ? "" : "disabled"}>刷新</button></div>
      <div class="field"><label for="cards-product">选择商品</label><select id="cards-product" ${role === "staff" || !products.length ? "disabled" : ""}>${products.map((product) => `<option value="${escape(product.id)}" ${product.id === selected ? "selected" : ""}>${escape(product.name)}</option>`).join("")}</select></div>
      <div id="cards-error" class="error" role="alert"></div>
      ${
        products.length
          ? `<div id="cards-stats" aria-live="polite"></div>
      <details class="panel"><summary>发行一批卡密</summary><p class="caption">卡密原文只在本次生成时显示。离开页面后不能找回，请立即下载保存。</p>
        <form id="cards-issue"><div class="grid"><div class="field"><label for="cards-issue-variant">${tr("制卡规格", "Variant to issue")}</label><select id="cards-issue-variant" required></select><p id="cards-issue-help" class="caption"></p></div><div class="field"><label for="cards-count">数量</label><input id="cards-count" type="number" min="1" max="1000" step="1" value="10" required></div><div class="field"><label for="cards-label">批次标签（可选）</label><input id="cards-label" maxlength="100" placeholder="例如：十月活动"></div><div class="field"><label for="cards-expires">兑换截止时间（可选，本地时间）</label><input id="cards-expires" type="datetime-local"></div></div><button id="cards-issue-submit" type="submit" class="full">生成卡密</button></form>
      </details><div id="cards-codes" class="secret-output"></div>
      <div class="form-divider"><h3>卡密库存与使用情况</h3><form id="cards-filter"><div class="grid"><div class="field"><label for="cards-variant">${tr("查看规格", "Filter by variant")}</label><select id="cards-variant"></select></div><div class="field"><label for="cards-status">状态</label><select id="cards-status"><option value="">全部状态</option>${Object.entries(
        labels,
      )
        .map(([value, label]) => `<option value="${value}">${label}</option>`)
        .join(
          "",
        )}</select></div><div class="field"><label for="cards-batch">批次 ID</label><input id="cards-batch" maxlength="100" placeholder="留空查看全部批次"></div><div class="field"><label for="cards-search">卡密 ID 或尾号</label><input id="cards-search" maxlength="128" autocomplete="off" placeholder="不需要输入完整卡密"></div></div><div class="actions"><button type="submit" class="secondary">筛选</button><button id="cards-reset" type="button" class="secondary">清空筛选</button></div></form>
      <div id="cards-inventory" aria-live="polite"></div><div class="toolbar"><button id="cards-previous" class="secondary" disabled>上一页</button><span id="cards-page" class="caption"></span><button id="cards-next" class="secondary" disabled>下一页</button></div></div><div id="cards-history"></div>`
          : '<div class="empty">先创建商品，再发行卡密。</div>'
      }`;
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
      disable(issueSelect, !enabled.length);
      disable(node("#cards-issue-submit"), !enabled.length);
      node("#cards-issue-help").textContent = enabled.length
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
        ["status", "#cards-status"],
        ["batch_id", "#cards-batch"],
        ["search", "#cards-search"],
      ]) {
        const value = node(selector).value.trim();
        if (value) params.set(key, value);
      }
      return params;
    };
    const overview = (stats) => {
      const summary = stats.summary || {};
      const metrics = [
        ["总发行", "total"],
        ["剩余未兑换", "remaining"],
        ["已提交兑换", "used"],
        ["正在处理", "in_progress"],
      ];
      node("#cards-stats").innerHTML =
        `<div class="grid cards-summary">${metrics.map(([label, key]) => `<div class="panel cards-metric"><p class="caption">${label}</p><h2>${count(summary[key])}</h2></div>`).join("")}</div>
        <p class="caption">剩余未兑换：尚未提交兑换且未到期。已提交后失败、可重试的卡密 ${count(summary.states?.failed_retryable)} 张另行统计，不计入未兑换数量。</p>
        <p class="caption">已验码 ${count(summary.verified)} · 已领取 ${count(summary.viewed)} · 已完成 ${count(summary.completed)} · 失败 ${count(summary.failed)} · 已撤销 ${count(summary.states?.revoked)} · 已过期 ${count(summary.states?.expired)}</p>`;
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
          `<details class="card-variant-summary" open><summary>${tr("各规格卡密统计", "Codes by variant")}</summary><div class="card-variant-grid">${stats.variants.map((variant) => `<article class="panel card-variant-metric"><h3>${escape(variant.name)}</h3><p class="caption">${escape(variant.price == null ? tr("未设置价格", "No price set") : `${variant.currency || "CNY"} ${variant.price}`)}${variant.enabled === false ? tr(" · 已停用", " · Disabled") : ""}</p><dl class="card-variant-counts"><div><dt>${tr("总发行", "Issued")}</dt><dd>${count(variant.summary?.total)}</dd></div><div><dt>${tr("未兑换卡密", "Unredeemed")}</dt><dd>${count(variant.summary?.remaining)}</dd></div></dl></article>`).join("")}</div><p class="caption">${tr(`规格合计：总发行 ${count(total)} 张，未兑换 ${count(remaining)} 张；均计入上方商品总量。`, `Variant totals: ${count(total)} issued, ${count(remaining)} unredeemed; included in the product totals above.`)}</p></details>`;
      }
    };
    const showInventory = (inventory) => {
      const items = Array.isArray(inventory.items) ? inventory.items : [];
      const total = Number(inventory.total || 0);
      node("#cards-inventory").innerHTML = items.length
        ? `<div class="table-wrap"><table class="card-inventory-table"><thead><tr><th>卡密 / 尾号</th><th>批次</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>${items.map((item) => `<tr><td class="mono" data-label="卡密 / 尾号">${escape(item.id)}<div class="caption">${item.code_suffix ? "····" + escape(item.code_suffix) : "旧卡密无尾号记录"}</div><div class="caption">${tr("规格", "Variant")}：${escape(item.variant_name || tr("默认规格", "Default variant"))}</div></td><td data-label="批次">${escape(item.batch_label || "—")}<div class="mono muted">${escape(item.batch_id || "")}</div></td><td data-label="状态">${status(String(item.status || ""))}${item.attempt ? `<p class="caption">第 ${count(item.attempt)} 次尝试</p>` : ""}</td><td data-label="时间"><div>发行 ${escape(date(item.created))}</div>${item.used_at ? `<div>兑换 ${escape(date(item.used_at))}</div>` : ""}${item.expires ? `<div class="caption">截止 ${escape(date(item.expires))}</div>` : '<div class="caption">无兑换截止时间</div>'}</td><td data-label="操作"><div class="row-tools"><button class="secondary" data-card-history="${escape(item.id)}">使用记录</button>${item.status === "unused" ? `<button class="danger" data-card-revoke="${escape(item.id)}">撤销</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>`
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
    const load = async () => {
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
          api(`${endpoint}/card-inventory?${query()}`, undefined, "GET", {
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
      showInventory(inventory);
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
      variantControls();
      offset = 0;
      node("#cards-status").value = "";
      node("#cards-batch").value = "";
      node("#cards-search").value = "";
      node("#cards-history").innerHTML = "";
      node("#cards-stats").innerHTML = "";
      node("#cards-inventory").innerHTML =
        '<div class="loading">正在读取这个商品的卡密…</div>';
      node("#cards-page").textContent = "";
      disable(node("#cards-previous"), true);
      disable(node("#cards-next"), true);
      onContextChange(selected);
      await load();
    });
    listen("#cards-refresh", "click", load);
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
        "#cards-batch",
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
            node("#cards-batch").value = result.batch_id;
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
        "delivery.viewed": "领取内容",
        "delivery.revealed": "领取内容",
        "delivery.destroyed": "销毁内容",
      }[type] || type
    );
  }

  window.ExtoreCards = Object.freeze({ render });
})();
