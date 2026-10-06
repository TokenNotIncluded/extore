"use strict";

// Receipt tokens and form drafts stay in memory. No card code is rendered or
// stored here; the exchange API returns only an index and a short suffix.
(() => {
  const sessions = new Map();
  const canonical = (value) => JSON.stringify(sort(value));
  function sort(value) {
    if (Array.isArray(value)) return value.map(sort);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sort(value[key])]));
  }
  const accepted = (item) => item.accepted !== false && Boolean(item.card_id && item.product);
  const flowOf = (item) => item.job?.task_flow || item.product?.task_flow_view || null;
  const fieldsOf = (item) => flowOf(item)?.enabled ? [] : item.product?.parameters || [];
  const attachment = (field) => ["file", "image", "images"].includes(field.type);
  const pending = (item) => accepted(item) && !item.job;

  // A matching key/type alone is insufficient: a frozen label, tutorial,
  // required flag or validation rule may give the field a different meaning.
  function groupItems(items, { drafts = new Map(), pendingOnly = false } = {}) {
    const products = new Map();
    for (const item of items || []) {
      if (!accepted(item) || (pendingOnly && !pending(item))) continue;
      const product = item.product;
      let productGroup = products.get(product.id);
      if (!productGroup) {
        productGroup = { product, groups: [] };
        products.set(product.id, productGroup);
      }
      const fields = fieldsOf(item);
      const saved = { ...(item.job?.params || {}), ...(drafts.get(item.card_id) || {}) };
      const defaults = Object.fromEntries(fields.filter((field) => !attachment(field))
        .map((field) => [field.key, saved[field.key] || ""]));
      const signature = canonical([fields, defaults, flowOf(item)?.definition_hash || null]);
      let group = productGroup.groups.find((entry) => entry.signature === signature);
      if (!group) {
        group = { signature, fields, defaults, flow: Boolean(flowOf(item)?.enabled), items: [], variants: [] };
        productGroup.groups.push(group);
      }
      group.items.push(item);
      const variantId = item.variant?.id || "";
      let variant = group.variants.find((entry) => entry.id === variantId);
      if (!variant) {
        variant = { id: variantId, name: item.variant?.name || "", items: [] };
        group.variants.push(variant);
      }
      variant.items.push(item);
    }
    return [...products.values()];
  }

  function session(data, deps) {
    const key = deps.token || "preview";
    if (!sessions.has(key)) {
      // Bound memory while keeping a recently uploaded file attached on retry.
      if (sessions.size >= 5) sessions.delete(sessions.keys().next().value);
      sessions.set(key, { drafts: new Map(), issues: [], selected: new Map(), uploads: new WeakMap() });
    }
    const state = sessions.get(key);
    if (data.items?.some((item) => !item.accepted))
      state.issues = data.items.filter((item) => !item.accepted).map((item) => ({ ...item }));
    return state;
  }

  function stateLabel(item, deps) {
    const { tr } = deps;
    if (!item.accepted) {
      return item.status === "duplicate" ? tr("重复，已跳过", "Duplicate, skipped")
        : item.status === "used" ? tr("已使用", "Already used") : tr("无效", "Invalid");
    }
    if (!item.job) return tr("可兑换", "Ready to redeem");
    if (item.status === "needs_retry") return tr("需要重试", "Retry needed");
    if (flowOf(item)?.phase === "await_start" || item.job.state === "waiting")
      return tr("等待你开始", "Waiting for you to start");
    const labels = {
      queued: ["排队中", "Queued"], processing: ["处理中", "Processing"],
      succeeded: ["已完成", "Completed"], failed: ["未完成", "Failed"],
      needs_input: ["需要重试", "Retry needed"], rejected: ["已拒绝", "Rejected"],
      destroyed: ["已销毁", "Destroyed"],
    };
    return tr(...(labels[item.job.state] || ["已提交", "Submitted"]));
  }

  function issuesMarkup(data, deps, state) {
    const { esc, tr } = deps;
    const issues = data.items?.filter((item) => !item.accepted) || [];
    const rows = issues.length ? issues : state.issues;
    if (!rows.length && !data.results?.some((result) => result.status === "error")) return "";
    return `<section class="batch-card" aria-label="${tr("校验与提交结果", "Validation and submission results")}"><h3>${tr("这些卡密需要检查", "These codes need attention")}</h3>${rows.map((item) => `<p class="caption">${tr("第", "Entry ")}${item.index + 1}${tr("张", "")} ···${esc(item.suffix || "????")} · ${esc(stateLabel(item, deps))}${item.error ? ` · ${esc(item.error)}` : ""}</p>`).join("")}${(data.results || []).filter((result) => result.status === "error").map((result) => {
      const card = data.items?.find((item) => item.card_id === result.card_id);
      return `<p class="error">${card ? `···${esc(card.suffix || "????")} · ` : ""}${esc(result.error || tr("这张卡密提交失败", "This code could not be submitted"))}</p>`;
    }).join("")}</section>`;
  }

  function receiptLink(deps) {
    if (!deps.token) return "";
    const { esc, tr } = deps;
    return `<div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${esc(deps.receiptURL)}</p><button id="copy" class="secondary" type="button">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("这个链接包含全部有效卡密，有效期 30 天，请勿转发。", "This link covers all valid codes. Valid for 30 days. Do not forward it.")}</p></div>`;
  }

  function variantsOf(groups) {
    const variants = new Map();
    for (const group of groups) for (const variant of group.variants) {
      if (!variants.has(variant.id)) variants.set(variant.id, { ...variant, items: [] });
      variants.get(variant.id).items.push(...variant.items);
    }
    return [...variants.values()];
  }

  function cardsMarkup(items, deps, { interactive = true } = {}) {
    const { esc, tr } = deps;
    return items.map((item) => {
      const flow = flowOf(item);
      const start = interactive && item.job && flow?.enabled && flow.actions?.includes("start");
      const progress = Math.max(0, Math.min(100, Number(item.job?.progress) || 0));
      return `<div class="batch-card"><div class="section-head"><h3>···${esc(item.suffix || "????")}</h3><span class="status ${esc(item.job?.state || "")}">${esc(stateLabel(item, deps))}</span></div>${item.job ? `<p class="caption">${esc(item.job.message || "")}${item.job.state === "waiting" ? "" : ` · ${progress}%`}</p>` : ""}${start ? `<p class="caption">${tr("只开始这一张，其他卡密会继续等待。", "Start this code only. The other codes keep waiting.")}</p><button type="button" class="secondary" data-batch-start="${esc(item.card_id)}">${tr("开始这张", "Start this code")}</button> ` : ""}${interactive ? `<button type="button" class="secondary" data-batch-open="${esc(item.card_id)}">${tr(item.job ? "查看这张" : "填写这张", item.job ? "Open this code" : "Fill in this code")}</button>` : ""}</div>`;
    }).join("");
  }

  function bindCommon(data, deps) {
    deps.on("#copy", deps.copy);
    deps.on("#batch-home", deps.home);
    deps.on("#batch-overview", deps.overview);
    for (const item of data.items || []) {
      if (!accepted(item)) continue;
      deps.on(`[data-batch-open="${item.card_id}"]`, () => deps.open(item));
      deps.on(`[data-batch-start="${item.card_id}"]`, async () => {
        const context = deps.context();
        const flow = flowOf(item);
        if (!flow?.actions?.includes("start")) return;
        await deps.api("/task-flow/start", {
          token: context.token, card_id: item.card_id,
          flow_epoch: flow.flow_epoch, expected_revision: flow.revision,
        }, "POST");
        if (context.active()) {
          const updated = await deps.refresh();
          const card = updated.items?.find((entry) => entry.card_id === item.card_id);
          if (context.active() && card?.job) deps.open(card);
        }
      });
    }
  }

  function renderOverview(data, deps) {
    const { esc, tr } = deps;
    const state = session(data, deps);
    const acceptedItems = (data.items || []).filter(accepted);
    const completed = acceptedItems.filter((item) => ["succeeded", "destroyed"].includes(item.job?.state)).length;
    const groups = groupItems(acceptedItems);
    deps.app.innerHTML = `<div class="narrow"><h1>${tr("批量兑换", "Batch redemption")}</h1><p class="caption">${tr(`已完成 ${completed} / ${acceptedItems.length} 张。每张卡密分别处理、领取。`, `${completed} of ${acceptedItems.length} completed. Each code has its own task and delivery.`)}</p><section class="panel"><div class="section-head"><h2>${tr("全部卡密", "All codes")}</h2>${acceptedItems.some(pending) ? `<button id="batch-fill" type="button" class="secondary">${tr("填写待兑换卡密", "Fill in pending codes")}</button>` : ""}</div>${groups.map(({ product, groups: schemas }) => `<section class="batch-started"><h3>${esc(product.name)}</h3>${variantsOf(schemas).map((variant) => `${variant.name ? `<h4>${esc(variant.name)}</h4>` : ""}${cardsMarkup(variant.items, deps)}`).join("")}</section>`).join("")}${issuesMarkup(data, deps, state)}<div id="error" class="error" role="alert"></div></section>${receiptLink(deps)}<button id="batch-home" class="secondary" type="button">${tr("兑换其他卡密", "Redeem other codes")}</button></div>`;
    bindCommon(data, deps);
    deps.on("#batch-fill", deps.edit);
  }

  function renderForm(data, deps) {
    const { esc, tr } = deps;
    const state = session(data, deps);
    const products = groupItems(data.items, { drafts: state.drafts, pendingOnly: true });
    if (!products.length) {
      renderOverview(data, deps);
      return;
    }
    const groups = products.flatMap((product) => product.groups);
    groups.forEach((group, index) => { group.index = index; });
    const selectedCards = () => groups.flatMap((group) => group.items)
      .filter((item) => deps.$(`#batch-select-${item.card_id}`)?.checked);
    const started = (data.items || []).filter((item) => accepted(item) && item.job);
    const pendingCount = groups.reduce((count, group) => count + group.items.length, 0);
    deps.app.innerHTML = `<div class="narrow"><h1>${tr("批量兑换", "Batch redemption")}</h1><p class="caption">${tr(`已验证 ${pendingCount} 张待兑换卡密，按商品和规格整理如下。`, `${pendingCount} codes are ready, grouped by product and variant.`)}</p><section class="panel"><div class="section-head"><h2>${tr("确认兑换信息", "Confirm your details")}</h2>${started.length ? `<button id="batch-overview" type="button" class="secondary">${tr("查看全部进度", "View all progress")}</button>` : ""}</div><p class="caption">${tr("同一商品、相同要求只填一次。附件始终按卡密分别上传；取消勾选的卡密不会提交。", "Fill compatible fields once within each product. Upload files separately for each code. Unchecked codes are not submitted.")}</p><form id="form" novalidate>${products.map(({ product, groups: schemas }) => `<section class="batch-started"><h3>${esc(product.name)}</h3>${schemas.length > 1 ? `<p class="caption">${tr("这些卡密的填写要求或已保存内容不同，已分开填写。", "These codes have different frozen requirements or saved details, so their fields are separate.")}</p>` : ""}${schemas.map((group) => `<section class="batch-card">${group.flow ? `<p>${tr("这是分步流程。确认兑换只准备任务；你可稍后逐张点击开始，填写当时出现的题目。", "This is a step-by-step flow. Confirming prepares the tasks. Start each code separately later and answer the questions shown then.")}</p>` : group.fields.filter((field) => !attachment(field)).map((field) => deps.parameterFields(`batch-param-${group.index}-`, field, group.defaults[field.key])).join("")}${group.variants.map((variant) => `${variant.name ? `<h4>${esc(variant.name)}</h4>` : ""}${variant.items.map((item) => {
      const saved = state.drafts.get(item.card_id) || {};
      const checked = state.selected.get(item.card_id) !== false;
      return `<section class="batch-card"><div class="checks"><label for="batch-select-${esc(item.card_id)}"><input id="batch-select-${esc(item.card_id)}" type="checkbox" ${checked ? "checked" : ""}> ${tr("兑换这张", "Redeem this code")} ···${esc(item.suffix || "????")}</label></div>${group.fields.filter((field) => attachment(field)).map((field) => deps.parameterFields(`batch-file-${item.card_id}-`, field, saved[field.key] || "")).join("")}<p id="batch-upload-error-${esc(item.card_id)}" class="error" role="status"></p></section>`;
    }).join("")}`).join("")}</section>`).join("")}</section>`).join("")}<button type="submit" class="full">${tr("确认兑换所选卡密", "Redeem selected codes")}</button><div id="error" class="error" role="alert"></div></form>${issuesMarkup(data, deps, state)}${started.length ? `<section class="batch-started"><h3>${tr("已提交的卡密", "Submitted codes")}</h3>${cardsMarkup(started, deps)}</section>` : ""}</section>${receiptLink(deps)}</div>`;
    bindCommon(data, deps);
    if (groups.some((group) => group.fields.some((field) => attachment(field)))) void deps.uploadFileLimit();
    for (const group of groups) for (const field of group.fields.filter((field) => !attachment(field))) {
      deps.$(`#batch-param-${group.index}-${field.key}`)?.addEventListener("input", (event) => {
        for (const item of group.items) state.drafts.set(item.card_id, {
          ...(state.drafts.get(item.card_id) || {}), [field.key]: event.target.value,
        });
      });
    }
    for (const group of groups) for (const item of group.items) {
      deps.$(`#batch-select-${item.card_id}`)?.addEventListener("change", (event) => {
        state.selected.set(item.card_id, Boolean(event.target.checked));
      });
    }
    deps.form(async () => {
      const context = deps.context();
      const selected = new Set(selectedCards().map((item) => item.card_id));
      if (!selected.size) throw new Error(tr("请至少选择一张卡密", "Select at least one code"));
      const requests = [];
      const errors = [];
      for (const group of groups) {
        const cards = group.items.filter((item) => selected.has(item.card_id));
        if (!cards.length) continue;
        const common = {};
        let valid = true;
        for (const field of group.fields.filter((field) => !attachment(field))) {
          const input = deps.$(`#batch-param-${group.index}-${field.key}`);
          common[field.key] = input?.value || "";
          if (!input?.reportValidity()) valid = false;
        }
        for (const item of cards) {
          const params = { ...common };
          const old = state.drafts.get(item.card_id) || {};
          const errorNode = deps.$(`#batch-upload-error-${item.card_id}`);
          if (errorNode) errorNode.textContent = "";
          try {
            if (!valid) throw new Error(tr("请检查这件商品的必填内容", "Check this product's required fields"));
            for (const field of group.fields.filter((field) => attachment(field))) {
              const input = deps.$(`#batch-file-${item.card_id}-${field.key}`);
              const files = deps.fieldFiles(input, field);
              const uploadKey = `${item.card_id}:${field.key}`;
              const retained = deps.$(`#batch-file-${item.card_id}-${field.key}-retained`)?.value || old[field.key] || "";
              if (!files.length && field.required && !retained) {
                input?.reportValidity();
                throw new Error(tr("请选择这张卡密需要的附件", "Choose this code's required file"));
              }
              const ids = [];
              for (const file of files) {
                if (!context.active()) return;
                let id = state.uploads.get(file)?.get(uploadKey);
                if (!id) {
                  id = (await deps.uploadMultipart("/files/upload", {
                    token: context.token, card_id: item.card_id, field_key: field.key,
                  }, file, { isCurrent: context.active })).id;
                  if (!context.active()) return;
                  if (!state.uploads.has(file)) state.uploads.set(file, new Map());
                  state.uploads.get(file).set(uploadKey, id);
                }
                ids.push(id);
              }
              params[field.key] = files.length
                ? field.type === "images" ? JSON.stringify(ids) : ids[0]
                : retained || (field.type === "images" ? "[]" : "");
              state.drafts.set(item.card_id, { ...params });
              if (!context.active()) return;
            }
            requests.push({ card_id: item.card_id, params });
          } catch (error) {
            if (!context.active()) return;
            errors.push({ card_id: item.card_id, suffix: item.suffix, error: error.message });
            if (errorNode) errorNode.textContent = error.message;
          }
          state.drafts.set(item.card_id, { ...old, ...params });
        }
      }
      if (!context.active()) return;
      if (!requests.length) throw new Error(tr("所选卡密尚未提交，请检查上方提示。", "No selected codes were submitted. Check the messages above."));
      // A failed upload belongs to one card. The successful cards can still be
      // submitted; its selected files/draft remain available for another try.
      await deps.submit(requests, errors);
    });
  }

  window.ExtoreBatchRedemption = { groupItems, pending, flowOf, renderForm, renderOverview };
})();
