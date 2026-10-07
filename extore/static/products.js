"use strict";

(() => {
  const views = new WeakMap();
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
  const localized = (value, lang) =>
    typeof value === "object" && value !== null
      ? value[lang] || value["zh-CN"] || Object.values(value)[0] || ""
      : value || "";
  const icon =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m4 7 8-4 8 4v10l-8 4-8-4zM4 7l8 4 8-4M12 11v10"/></svg>';
  const markdown = (value) => {
    const text = String(value || "");
    if (!window.DOMPurify?.sanitize || !window.marked?.parse)
      return `<div class="markdown"><p>${escape(text)}</p></div>`;
    return `<div class="markdown">${window.DOMPurify.sanitize(
      window.marked.parse(text),
      {
        FORBID_TAGS: ["img", "style", "iframe", "form", "input"],
        FORBID_ATTR: ["style", "id"],
      },
    )}</div>`;
  };
  const tutorial = (definition, lang, label) => {
    const text = localized(definition.description, lang);
    return text
      ? `<details ${definition.collapsed ? "" : "open"}><summary>${escape(label)}</summary>${markdown(text)}</details>`
      : "";
  };
  const defaultOutput = () => ({
    key: "content",
    label: { "zh-CN": "交付内容", en: "Delivery content" },
    description: {},
    type: "textarea",
    required: true,
    collapsed: true,
  });
  const defaultVariant = () => ({
    id: "default",
    name: "默认规格",
    description: "",
    price: null,
    currency: "CNY",
    attributes: {},
    enabled: true,
  });
  const productVariants = (product) =>
    Array.isArray(product.variants) && product.variants.length
      ? product.variants
      : [defaultVariant()];
  const isDeleted = (product) => product?.deleted === true || Boolean(product?.deleted_at);
  let generatedId = 0;
  const newItemId = (prefix) => {
    const random = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID().replaceAll("-", "").slice(0, 12)
      : Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    return prefix + "_" + random + "_" + (++generatedId).toString(36);
  };
  const priceLabel = (variant) =>
    variant.price === null || variant.price === undefined
      ? "未设置参考价"
      : `参考价 ${variant.price} ${variant.currency || "CNY"}`;
  const inputTypes = [
    ["text", "文本"],
    ["email", "邮箱"],
    ["url", "链接"],
    ["textarea", "多行文本"],
    ["number", "数字"],
    ["file", "文件（单个文件，最多 20 MiB）"],
    ["select", "下拉选项"],
    ["boolean", "是 / 否"],
    ["image", "单张图片（PNG、JPEG、WebP）"],
    ["images", "图片集合（最多 20 张）"],
  ];
  const field = (id, label, value = "", type = "text", attributes = "") =>
    `<div class="field"><label for="${id}">${escape(label)}</label><input id="${id}" type="${type}" value="${escape(value)}" ${attributes}></div>`;
  const textarea = (id, label, value = "", attributes = "") =>
    `<div class="field"><label for="${id}">${escape(label)}</label><textarea id="${id}" ${attributes}>${escape(value)}</textarea></div>`;
  const select = (id, label, options, value, attributes = "") =>
    `<div class="field"><label for="${id}">${escape(label)}</label><select id="${id}" ${attributes}>${options.map(([key, text]) => `<option value="${escape(key)}" ${value === key ? "selected" : ""}>${escape(text)}</option>`).join("")}</select></div>`;

  function begin(ctx, name) {
    const view = { name };
    views.set(ctx.workspace, view);
    const active = () => ctx.isCurrent() && views.get(ctx.workspace) === view;
    const $ = (selector) => ctx.workspace.querySelector(selector);
    const all = (selector) => ctx.workspace.querySelectorAll(selector);
    const report = (error) => {
      if (!active()) return;
      const node = $("#error");
      if (node) node.textContent = error.message || String(error);
      else ctx.notify(error.message || String(error));
    };
    const perform = async (handler, node) => {
      if (!active()) return;
      if (node) node.disabled = true;
      try {
        const error = $("#error");
        if (error) error.textContent = "";
        await handler();
      } catch (error) {
        report(error);
      } finally {
        if (active() && node?.isConnected) node.disabled = false;
      }
    };
    const on = (selector, event, handler) => {
      const node = $(selector);
      node?.addEventListener(event, (eventObject) => {
        if (event === "submit") eventObject.preventDefault();
        perform(() => handler(eventObject), event === "click" ? node : null);
      });
    };
    return { active, $, all, perform, on, report };
  }

  function exportHandler(ctx, view) {
    const { active, $, on } = view;
    let revision = 0;
    return async (productId) => {
      const loadId = ++revision;
      const current = () => active() && revision === loadId;
      const exporter = window.ExtoreProductExport;
      if (!exporter) throw new Error("商品导出模块未加载，请刷新页面");
      const canInspectCards = ctx.role === "admin" || ctx.canManageCards === true;
      const productRequest = ctx.role === "staff"
        ? ctx.api("/manage/product")
        : ctx.api("/admin/products").then((values) =>
            values.find((product) => product.id === productId),
          );
      const statsRequest = canInspectCards
        ? ctx.api((ctx.role === "staff" ? "/manage" : "/admin") +
            "/card-stats?" + new URLSearchParams({ product_id: productId }))
            .catch(() => null)
        : Promise.resolve(null);
      let saved, stats;
      try {
        [saved, stats] = await Promise.all([productRequest, statsRequest]);
      } catch (error) {
        if (current()) throw error;
        return;
      }
      if (!current()) return;
      if (!saved || saved.id !== productId)
        throw new Error("这个商品已不存在，无法复制资料");
      const options = {
        lang: ctx.lang,
        inventory: stats?.variants,
        isCurrent: current,
        notify: ctx.notify,
      };
      const panel = $("#product-export-panel");
      panel.innerHTML = `<details class="product-export-panel" open><summary>${ctx.lang === "en" ? "Saved product information for AI" : "已保存的商品资料 · AI 提示词"}</summary><p class="caption">${ctx.lang === "en" ? "This contains saved product information. Fulfillment secrets, private processor configuration, full codes and management links are excluded." : "资料来自已保存的商品。不包含发货密钥、处理器私密配置、完整卡密或管理链接。"}</p><p class="caption">${stats?.variants ? (ctx.lang === "en" ? "Counts refer to unredeemed codes, not unsold units; previously sold codes can still be unredeemed." : "统计的是未兑换卡密数量，不是未售库存；在其他平台已售出但未兑换的卡密仍会计入。") : (ctx.lang === "en" ? "Code counts were not provided. No inventory quantity is assumed." : "未提供卡密统计，不会假定库存数量。")}</p>${textarea("product-export-text", ctx.lang === "en" ? "Product prompt" : "商品 AI 提示词", exporter.prompt(saved, options), 'readonly spellcheck="false" class="product-export-text"')}<button type="button" id="copy-product-prompt" class="secondary">${ctx.lang === "en" ? "Copy prompt" : "复制提示词"}</button></details>`;
      options.textarea = $("#product-export-text");
      on("#copy-product-prompt", "click", () => exporter.copy(saved, options));
      await exporter.copy(saved, options);
    };
  }

  async function render(ctx, createdLink = null) {
    if (!ctx.isCurrent()) return;
    const view = begin(ctx, "list");
    const { active, $, all, on, report, perform } = view;
    const exportProduct = exportHandler(ctx, view);
    const owner = ctx.role === "admin";
    const tr = (zh, en) => ctx.lang === "en" ? en : zh;
    const productView = ["active", "deleted", "all"].includes(ctx.productView) ? ctx.productView : "active";
    const products = (ctx.products || []).filter((product) => productView === "all" || isDeleted(product) === (productView === "deleted"));
    const activeProducts = products.filter((product) => !isDeleted(product));
    const canDelete = owner || ctx.canDelete === true;
    const canEdit = owner || ctx.canEdit !== false;
    const canCreate = owner && productView !== "deleted";
    ctx.workspace.innerHTML = `<div class="section-head"><h2>${tr("商品", "Products")}</h2>${canCreate ? `<button id="new-product">${tr("新建商品", "New product")}</button>` : ""}</div>
      <div class="toolbar product-lifecycle-toolbar">${select("products-view", tr("商品视图", "Product view"), [["active", tr("在售商品", "Active products")], ["deleted", tr("回收站", "Recycle bin")], ["all", tr("全部商品", "All products")]], productView)}</div><div id="product-lifecycle-confirmation" aria-live="polite"></div>
      ${canCreate ? `<div class="form-divider"><div class="grid">${select("quick-template", "快速新建模板", [["random", "随机选择模板"]], "random")}<div class="field"><label for="quick-product">生成私有草稿与 AI 配置链接</label><button id="quick-product" class="secondary" disabled>随机快速新建</button></div></div><p class="caption">先创建一个私有商品，再把仅能配置这个商品的链接交给 AI 完善。配置链接有效期为 7 天。复制已有商品后，需重新填写私密发货配置。</p></div>` : ""}
      ${createdLink ? `<div class="parameter" id="quick-created"><h3>商品已创建 · ${escape(createdLink.productName)}</h3><p class="caption">这个链接只允许编辑当前商品与发货配置。请复制保存；离开后完整链接不再显示。</p>${field("quick-management-link", "AI 商品配置链接", createdLink.url, "text", "readonly")}<div class="toolbar"><button id="copy-quick-link" class="secondary">${ctx.lang === "en" ? "Copy link" : "复制链接"}</button><button id="edit-quick-product" class="secondary">继续配置商品</button></div></div>` : ""}
      ${products.length ? `<div class="product-list">${products.map((product) => `<article class="product-row">${product.logo ? `<img src="${escape(product.logo)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${escape(product.name)}</h3><p>${isDeleted(product) ? tr("已删除 · 旧卡密与任务仍有效", "Deleted · existing codes and tasks remain valid") : `${product.public ? "公开展示" : "仅持卡可见"} · ${escape({ manual: "队列", stock: "一卡一文本", webhook: "外部 Webhook", script: "商品处理器" }[product.mode] || product.mode)} · ${product.delivery === "service" ? "服务状态" : "内容交付"}`}</p><div class="variant-summary">${productVariants(product).map((variant) => `<span>${escape(variant.name)} · ${escape(priceLabel(variant))}${variant.enabled === false ? " · 已停用" : ""}</span>`).join("")}</div><div class="mono muted">${escape(product.id)}</div></div><div class="product-actions">${isDeleted(product) ? (canDelete ? `<button type="button" class="secondary" data-restore="${escape(product.id)}">${tr("恢复商品", "Restore product")}</button>` : "") : `${canEdit ? `<button class="secondary" data-edit="${escape(product.id)}">${tr("配置", "Configure")}</button><button type="button" class="secondary" data-export="${escape(product.id)}">${tr("复制商品资料", "Copy product info")}</button>` : ""}${canDelete ? `<button type="button" class="danger" data-delete="${escape(product.id)}">${tr("删除商品", "Delete product")}</button>` : ""}`}</div></article>`).join("")}</div>` : `<div class="empty">${productView === "deleted" ? tr("回收站暂无商品。", "The recycle bin is empty.") : tr("暂无商品。", "No products here.")}</div>`}
      <div id="product-export-panel"></div>
      <div id="error" class="error" role="alert"></div>`;
    let confirmation = 0;
    const reload = async (selectedView = productView) => {
      if (!active()) return;
      if (ctx.onViewChange) return ctx.onViewChange(selectedView, ctx.products);
      const values = await ctx.api((owner ? "/admin" : "/manage") + "/products?" + new URLSearchParams({ view: selectedView }), undefined, "GET");
      if (!active()) return;
      ctx.productView = selectedView;
      ctx.products = values;
      ctx.onSaved(values);
      return render(ctx);
    };
    on("#products-view", "change", () => reload($("#products-view").value));
    const lifecycle = async (product, deleted) => {
      if (!active() || !canDelete || isDeleted(product) === deleted) return;
      const generation = ++confirmation;
      const panel = $("#product-lifecycle-confirmation");
      panel.innerHTML = `<section class="product-lifecycle-confirmation"><h3>${escape(tr("删除商品：", "Delete product: ") + product.name)}</h3><p>${tr("商品会移入回收站，不再公开展示或发行新卡密。已有卡密、领取链接与未完成任务仍然有效；恢复后可以继续使用。", "The product moves to the recycle bin and stops public listing and new code issuance. Existing codes, receipt links and unfinished tasks remain valid. Restore it to continue configuring and issuing codes.")}</p><div class="actions"><button type="button" class="danger" id="confirm-product-delete">${tr("确认移入回收站", "Confirm move to recycle bin")}</button><button type="button" class="secondary" id="cancel-product-delete">${tr("取消", "Cancel")}</button></div></section>`;
      on("#cancel-product-delete", "click", () => { confirmation++; panel.innerHTML = ""; });
      on("#confirm-product-delete", "click", async () => {
        if (!active() || generation !== confirmation || !canDelete) return;
        const cancel = $("#cancel-product-delete");
        if (cancel) cancel.disabled = true;
        const endpoint = owner ? "/admin/products/" + encodeURIComponent(product.id) : "/manage/product?" + new URLSearchParams({ product_id: product.id });
        let result;
        try { result = await ctx.api(endpoint, { confirmed: true }, "DELETE"); }
        finally { if (active() && generation === confirmation && cancel?.isConnected) cancel.disabled = false; }
        if (!active() || generation !== confirmation) return;
        if (result?.ok !== true || result.product_id !== product.id || result.deleted !== true) throw new Error(tr("未能确认删除结果，请刷新商品列表。", "Could not confirm deletion. Refresh the product list."));
        ctx.products = ctx.products.map((value) => value.id === product.id ? { ...value, deleted: true, deleted_at: result.deleted_at } : value);
        ctx.onSaved(ctx.products);
        ctx.notify(tr("商品已移入回收站，旧卡密与任务仍有效。", "Product moved to the recycle bin. Existing codes and tasks remain valid."));
        await reload();
      });
    };
    all("[data-delete]").forEach((node) => node.addEventListener("click", () => perform(() => lifecycle(products.find((product) => product.id === node.dataset.delete), true), node)));
    all("[data-restore]").forEach((node) => node.addEventListener("click", () => perform(async () => {
      const product = products.find((product) => product.id === node.dataset.restore);
      if (!active() || !canDelete || !isDeleted(product)) return;
      const endpoint = owner ? "/admin/products/" + encodeURIComponent(product.id) + "/restore" : "/manage/product/restore?" + new URLSearchParams({ product_id: product.id });
      const result = await ctx.api(endpoint, {}, "POST");
      if (!active()) return;
      if (result?.ok !== true || result.product_id !== product.id || result.deleted !== false) throw new Error(tr("未能确认恢复结果，请刷新商品列表。", "Could not confirm restoration. Refresh the product list."));
      ctx.products = ctx.products.map((value) => value.id === product.id ? { ...value, deleted: false, deleted_at: null } : value);
      ctx.onSaved(ctx.products);
      ctx.notify(tr("商品已恢复。", "Product restored."));
      await reload();
    }, node)));
    on("#new-product", "click", () => edit(ctx));
    all("[data-edit]").forEach((node) =>
      node.addEventListener("click", () => {
        perform(async () => {
          if (!active() || !canEdit) return;
          const source = ctx.role === "staff" ? await ctx.api("/manage/product", undefined, "GET") : products.find((product) => product.id === node.dataset.edit);
          if (!active()) return;
          if (!source || source.id !== node.dataset.edit || isDeleted(source)) throw new Error(tr("商品已删除或权限已改变，请刷新商品列表。", "The product was deleted or its permissions changed. Refresh the list."));
          await edit(ctx, source);
        }, node);
      }),
    );
    all("[data-export]").forEach((node) =>
      node.addEventListener("click", () =>
        perform(() => exportProduct(node.dataset.export), node),
      ),
    );
    if (createdLink) {
      on("#copy-quick-link", "click", async () => {
        const input = $("#quick-management-link");
        if (ctx.copyManagementLink) {
          await ctx.copyManagementLink(createdLink.url, input, active);
          return;
        }
        if (globalThis.isSecureContext !== false && navigator.clipboard?.writeText) {
          try {
            await navigator.clipboard.writeText(createdLink.url);
            if (active()) ctx.notify(ctx.lang === "en" ? "Link copied" : "配置链接已复制");
            return;
          } catch {
            // A browser clipboard denial still allows manual copying.
          }
        }
        if (!active()) return;
        input.focus();
        input.select();
        ctx.notify(ctx.lang === "en" ? "Could not copy. The link is selected; copy it manually." : "请复制已选中的配置链接");
      });
      on("#edit-quick-product", "click", () =>
        edit(
          ctx,
          products.find((product) => product.id === createdLink.productId),
        ),
      );
    }
    ctx.refreshTools();
    if (!canCreate) return;
    let templates;
    try {
      const response = await ctx.api("/admin/product-templates");
      templates = Array.isArray(response) ? response : response.templates || [];
    } catch (error) {
      report(error);
      return;
    }
    if (!active()) return;
    $("#quick-template").innerHTML =
      `<option value="random">随机选择模板</option>${templates.map((template) => `<option value="${escape(template.id)}">${escape(localized(template.name, ctx.lang))}</option>`).join("")}${activeProducts.map((product) => `<option value="existing_product:${escape(product.id)}">复制：${escape(product.name)}</option>`).join("")}`;
    $("#quick-product").disabled = !templates.length && !activeProducts.length;
    on("#quick-product", "click", async () => {
      const chosen = $("#quick-template").value;
      let body;
      if (chosen.startsWith("existing_product:")) {
        body = {
          template_id: "existing_product",
          from_product_id: chosen.slice("existing_product:".length),
        };
      } else {
        const templateId =
          chosen === "random"
            ? templates[Math.floor(Math.random() * templates.length)]?.id
            : chosen;
        if (!templateId) throw new Error("没有可用的快速新建模板");
        body = { template_id: templateId };
      }
      const result = await ctx.api("/admin/products/quick", body);
      // Saving is already committed even if the user left during the request.
      const updated = [
        ...ctx.products.filter((product) => product.id !== result.product.id),
        result.product,
      ];
      ctx.products = updated;
      ctx.onSaved(updated);
      if (!active()) return;
      await render(ctx, {
        productId: result.product.id,
        productName: result.product.name,
        url: result.management_link.url,
      });
      if (ctx.isCurrent()) ctx.notify("私有商品草稿已创建");
    });
  }

  async function edit(ctx, source) {
    if (!ctx.isCurrent()) return;
    if (isDeleted(source) || (ctx.role !== "admin" && ctx.canEdit === false)) {
      ctx.notify(ctx.lang === "en" ? "Restore the product or obtain editing permission before configuring it." : "请先恢复商品或取得编辑权限，再配置商品。");
      return render(ctx);
    }
    const view = begin(ctx, "editor");
    const { active, $, all, perform, on, report } = view;
    const exportProduct = exportHandler(ctx, view);
    const product = structuredClone(
      source || {
        name: "",
        description: "",
        parameters: [],
        outputs: [defaultOutput()],
        mode: "manual",
        delivery: "content",
        view_policy: "repeat",
        allow_retry: true,
        max_attempts: 3,
        processor_id: "",
        processor_config: {},
      },
    );
    let parameters = structuredClone(product.parameters || []);
    let outputs = structuredClone(
      product.outputs ||
        (product.delivery === "service" ? [] : [defaultOutput()]),
    );
    let catalog = [];
    let processorId = product.processor_id || "";
    let processorConfig = { ...(product.processor_config || {}) };
    let displayedProcessorId = "";
    const configByProcessor = new Map([[processorId, processorConfig]]);
    let previousMode = product.mode;
    let previousDelivery = product.delivery;
    let customParameters = structuredClone(parameters);
    let customOutputs = structuredClone(outputs);
    let variants = structuredClone(productVariants(product));
    let progressSteps = structuredClone(product.progress_steps || []);
    const owner = ctx.role === "admin";
    const disabled = ctx.canConfigure ? "" : "disabled";
    let profiles = [], profileBinding = null, bindingLoad = 0, profilesRequested = false;
    ctx.workspace.innerHTML = `<div class="section-head"><h2>${product.id ? "配置商品" : "新建商品"}</h2><div class="product-actions">${product.id ? `<button type="button" id="export-saved-product" class="secondary">${ctx.lang === "en" ? "Copy saved product info" : "复制已保存商品资料"}</button>` : ""}<button id="cancel" class="secondary">返回商品</button></div></div><div id="product-export-panel"></div><form id="product-form">
      <div class="grid">${field("p-name", "商品名称", product.name)}${select(
        "p-mode",
        "处理方式",
        [
          ["manual", "队列"],
          ["stock", "一卡一文本 · 自动交付"],
          ["webhook", "外部 Webhook"],
          ["script", "商品处理器"],
        ],
        product.mode,
        disabled,
      )}${field("p-logo", "商品 Logo URL（HTTPS）", product.logo || "", "url")}${field("p-image", "商品图片 URL（HTTPS）", product.image || "", "url")}${select(
        "p-delivery",
        "交付类型",
        [
          ["content", "交付内容 / 链接"],
          ["service", "只返回服务状态"],
        ],
        product.delivery,
        disabled,
      )}${select(
        "p-view",
        "内容查看规则",
        [
          ["repeat", "允许重复查看"],
          ["once", "仅允许领取一次"],
        ],
        product.view_policy,
        disabled,
      )}</div>
      ${textarea("p-description", "商品描述（Markdown）", product.description)}
      ${field("p-support-email", "商家催办邮箱（可留空）", product.support_email || "", "email", 'maxlength="254" autocomplete="email"')}
      <div class="form-divider" id="product-progress-section"><div class="section-head"><h3>处理步骤</h3><button type="button" id="add-progress-step" class="secondary">添加步骤</button></div><p class="caption">按处理顺序配置步骤，顾客可跟踪每一步的状态。修改只用于之后的任务，正在处理的任务会保留原来的步骤。</p><div id="product-progress-steps"></div></div>
      <div class="form-divider" id="product-task-flow"></div>
      <div class="form-divider"><div class="section-head"><h3>规格 / 档位</h3><button type="button" id="add-variant" class="secondary">添加规格</button></div><p class="caption">每个规格有独立的卡密库存，数量在卡密页查看。参考价供外部商城配置参考；Extore 只负责兑换与交付，不收款。</p><div id="product-variants"></div><p class="caption">规格标识固定。已有卡密的规格不能删除，可停用，避免继续发行。</p></div>
      <div class="checks"><label><input id="p-public" type="checkbox" ${product.public ? "checked" : ""}>公开展示商品</label><label><input id="p-retry" type="checkbox" ${product.allow_retry ? "checked" : ""} ${disabled}>允许明确失败后重试</label></div>
      ${field("p-attempts", "最多尝试次数", product.max_attempts, "number", `${disabled} min="1" max="20"`)}
      <div class="form-divider" id="delivery-connection"><h3>发货对接</h3>
        <p id="queue-help" class="caption">队列任务可以由人员或 AI 领取处理。下方定义顾客填写的信息，以及完成任务时必须提交的结果。</p>
        <p id="stock-help" class="caption" hidden>保存商品后，在「卡密」页粘贴文本或导入 UTF-8 文件，一行生成一张卡密。顾客兑换后直接领取对应文本，无需排队。</p>
        <div id="webhook-settings"><p class="caption">将任务交给外部平台处理。接收地址须为 HTTPS 公网地址，使用签名密钥验证任务与回调。</p><div class="grid">${field("p-url", "Webhook 接收地址", product.webhook_url || "", "url", disabled)}${field("p-secret", "Webhook 签名密钥（至少 32 字符）", product.webhook_secret || "", "password", `${disabled} autocomplete="new-password"`)}</div></div>
        <div id="processor-settings">${select("p-processor", "商品处理器", [["", "正在加载处理器…"]], "", owner ? disabled : "disabled")}<p id="processor-description" class="caption"></p><div id="processor-configuration"></div><p class="caption">顾客填写项与交付结果由处理器代码定义。这里只能选择商品处理器预设并填写它声明的配置。</p></div>
      </div>
      <div class="form-divider"><div class="section-head"><h3>顾客填写的信息</h3><button type="button" id="add-param" class="secondary">添加参数</button></div><div id="parameters"></div></div>
      <div class="form-divider" id="outputs-section"><div class="section-head"><h3>任务完成时提交的结果</h3><button type="button" id="add-output" class="secondary" ${disabled}>添加输出字段</button></div><p class="caption">结果只在顾客主动领取时显示。人员、AI 或外部平台提交完成结果时，都须符合这些定义。</p><div id="outputs"></div></div>
      <p class="caption">队列商品调整输入输出后，新任务采用新定义，已提交任务保留原定义。自动处理商品发行卡密后，处理方式与输入输出结构不能更换。</p><button type="submit" id="save-product" class="full">保存商品</button><div id="error" class="error" role="alert"></div>
    </form>`;

    const taskFlowEditor = window.ExtoreTaskFlowEditor?.mount($("#product-task-flow"), {
      value: product.task_flow || null,
      disabled: !ctx.canConfigure,
      active,
      onChange: (enabled) => {
        const section = $("#parameters")?.closest(".form-divider");
        if (section) section.hidden = enabled;
      },
      confirm: (message) => globalThis.confirm(message),
      onPreset: () => {
        captureCustom();
        // The built-in graph ends with the conventional content field. A
        // merchant can change both final outputs and the end mapping afterward.
        if ($("#p-mode").value !== "script" && previousDelivery === "content" &&
            !outputs.some((definition) => definition.key === "content")) {
          outputs.push(defaultOutput());
          customOutputs = structuredClone(outputs);
          drawFields("#outputs", "o", outputs, !ctx.canConfigure, false);
        }
      },
    });
    if (!taskFlowEditor && product.task_flow) {
      $("#product-task-flow").innerHTML = '<p class="error">任务编排模块未加载。刷新页面后再编辑编排；其他商品资料仍可保存。</p>';
    }

    const parseObject = (id, label) => {
      try {
        const value = JSON.parse($("#" + id).value);
        if (!value || Array.isArray(value) || typeof value !== "object")
          throw new Error("expected object");
        if (Object.values(value).some((text) => typeof text !== "string"))
          throw new Error("expected strings");
        return value;
      } catch {
        throw new Error(`${label}需要填写“语言 → 文本”的 JSON 对象`);
      }
    };
    const captureVariants = () => {
      variants = variants.map((variant, index) => {
        const price = $("#v-price-" + index).value.trim();
        const currency = $("#v-currency-" + index).value.trim().toUpperCase() || "CNY";
        if (!/^[A-Z]{3,5}$/.test(currency))
          throw new Error(`规格 ${index + 1} 的币种须为 3 至 5 位字母，例如 CNY 或 USDT`);
        if (price && (price.length > 100 || !/^[0-9]+(?:\.\d{1,6})?$/.test(price) ||
          (price.split(".")[0].replace(/^0+/, "") || "0").length > 12))
          throw new Error(`规格 ${index + 1} 的参考价必须是非负金额，最多 12 位整数、6 位小数`);
        let attributes;
        try {
          attributes = JSON.parse($("#v-attributes-" + index).value);
          if (!attributes || typeof attributes !== "object" || Array.isArray(attributes) ||
              Object.keys(attributes).length > 20 ||
              Object.keys(attributes).some((key) => !key.trim() || key.length > 100) ||
              Object.values(attributes).some((value) => value !== null &&
                !(typeof value === "string" && value.length <= 1000) && typeof value !== "boolean" &&
                !(typeof value === "number" && Number.isFinite(value) &&
                  (!Number.isInteger(value) || Number.isSafeInteger(value)))))
            throw new Error("invalid attributes");
        } catch {
          throw new Error(`规格 ${index + 1} 的属性需要 JSON 对象，最多 20 项；值可用文字、数字、布尔值或 null，大整数请用字符串`);
        }
        return {
          ...variant,
          id: variant.id,
          name: $("#v-name-" + index).value,
          description: $("#v-description-" + index).value,
          price: price || null,
          currency,
          attributes,
          enabled: $("#v-enabled-" + index).checked,
        };
      });
    };
    const drawVariants = () => {
      $("#product-variants").innerHTML = variants.map((variant, index) =>
        `<section class="variant-editor"><div class="section-head"><h4>规格 ${index + 1}</h4>${variant.id === "default" || variants.length === 1 ? "" : `<button type="button" class="danger" data-remove-variant="${index}">删除规格</button>`}</div><div class="variant-fields">${field("v-name-" + index, "规格名称", variant.name, "text", 'maxlength="120"')}${field("v-id-" + index, "固定规格标识", variant.id, "text", "readonly")}${field("v-price-" + index, "参考价（可留空）", variant.price ?? "", "text", 'inputmode="decimal" placeholder="例如 19.90"')}${field("v-currency-" + index, "币种", variant.currency || "CNY", "text", 'maxlength="5" autocapitalize="characters"')}</div>${textarea("v-description-" + index, "规格说明", variant.description || "")}${textarea("v-attributes-" + index, "规格属性（JSON）", JSON.stringify(variant.attributes || {}, null, 2))}<p class="caption">例如：{"duration_months": 1, "tier": "basic"}，处理任务时会带上这些属性。</p><label class="variant-enabled"><input id="v-enabled-${index}" type="checkbox" ${variant.enabled !== false ? "checked" : ""}>启用此规格</label></section>`,
      ).join("");
      all("[data-remove-variant]").forEach((node) =>
        node.addEventListener("click", () => perform(() => {
          captureVariants();
          variants.splice(Number(node.dataset.removeVariant), 1);
          drawVariants();
        }, node)),
      );
    };
    drawVariants();
    const captureProgressSteps = (validate = true) => {
      progressSteps = progressSteps.map((step, index) => {
        const label = { ...step.label };
        for (const [locale, suffix] of [["zh-CN", "zh"], ["en", "en"]]) {
          const value = $("#ps-label-" + suffix + "-" + index).value.trim();
          if (validate && value.length > 200)
            throw new Error(`步骤 ${index + 1} 的名称最多 200 个字符`);
          if (value) label[locale] = value;
          else delete label[locale];
        }
        if (validate && !Object.keys(label).length)
          throw new Error(`请填写步骤 ${index + 1} 的中文或英文名称`);
        if (validate && Object.keys(label).length > 20)
          throw new Error(`步骤 ${index + 1} 的名称最多支持 20 种语言`);
        return { id: step.id, label };
      });
    };
    const drawProgressSteps = () => {
      $("#product-progress-steps").innerHTML = progressSteps.length
        ? `<ol class="progress-step-editor">${progressSteps.map((step, index) => `<li class="parameter"><div class="section-head"><h4>步骤 ${index + 1}</h4><div class="product-actions"><button type="button" class="secondary" data-step-up="${index}" ${index === 0 ? "disabled" : ""}>上移</button><button type="button" class="secondary" data-step-down="${index}" ${index === progressSteps.length - 1 ? "disabled" : ""}>下移</button><button type="button" class="danger" data-step-remove="${index}">删除</button></div></div><div class="variant-fields">${field("ps-label-zh-" + index, "中文名称", step.label?.["zh-CN"] || "", "text", 'maxlength="200"')}${field("ps-label-en-" + index, "English name", step.label?.en || "", "text", 'maxlength="200"')}</div>${field("ps-id-" + index, "固定步骤标识", step.id, "text", "readonly")}</li>`).join("")}</ol>`
        : '<p class="caption">未配置处理步骤，顾客仍可查看任务状态与进度。</p>';
      for (const [attribute, action] of [["step-up", "up"], ["step-down", "down"], ["step-remove", "remove"]]) {
        all(`[data-${attribute}]`).forEach((node) =>
          node.addEventListener("click", () => perform(() => {
            if (node.disabled) return;
            captureProgressSteps(false);
            const index = Number(node.getAttribute("data-" + attribute));
            if (action === "remove") progressSteps.splice(index, 1);
            else {
              const target = index + (action === "up" ? -1 : 1);
              if (target < 0 || target >= progressSteps.length) return;
              [progressSteps[index], progressSteps[target]] = [progressSteps[target], progressSteps[index]];
            }
            drawProgressSteps();
          })),
        );
      }
    };
    drawProgressSteps();
    const captureFields = (prefix, fields) => fields.map((definition, index) => {
      const next = {
        ...definition,
        key: $("#" + prefix + "-key-" + index).value,
        label: parseObject(prefix + "-label-" + index, "显示名称"),
        description: parseObject(prefix + "-description-" + index, "填写教程"),
        type: $("#" + prefix + "-type-" + index).value,
        required: $("#" + prefix + "-required-" + index).checked,
        collapsed: $("#" + prefix + "-collapsed-" + index).checked,
      };
      if (next.type === "select") {
        let choices;
        try { choices = JSON.parse($("#" + prefix + "-options-" + index).value); }
        catch { throw new Error("下拉选项须为 JSON 数组，请检查格式。"); }
        if (!Array.isArray(choices) || !choices.length || choices.length > 100 || choices.some((choice) =>
          !choice || typeof choice !== "object" || Array.isArray(choice) || Object.keys(choice).some((key) => !["value", "label"].includes(key)) ||
          typeof choice.value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(choice.value) ||
          !choice.label || typeof choice.label !== "object" || Array.isArray(choice.label) || !Object.keys(choice.label).length || Object.keys(choice.label).length > 20 ||
          Object.entries(choice.label).some(([locale, text]) => !locale || locale.length > 40 || typeof text !== "string" || !text.trim() || text.length > 200)) || new Set(choices.map((choice) => choice.value)).size !== choices.length)
          throw new Error("请设置 1–100 个下拉选项，每项包含唯一的 value 和多语言 label。");
        next.options = choices;
      } else delete next.options;
      if (next.type === "images") {
        const maximum = Number($("#" + prefix + "-max-items-" + index).value);
        if (!Number.isInteger(maximum) || maximum < 1 || maximum > 20) throw new Error("图片集合数量须为 1–20 的整数。");
        next.max_items = maximum;
      } else delete next.max_items;
      return next;
    });
    const captureCustom = () => {
      if (!["script", "stock"].includes(previousMode)) {
        parameters = captureFields("f", parameters);
        if (previousDelivery === "content")
          outputs = captureFields("o", outputs);
        customParameters = structuredClone(parameters);
        customOutputs = structuredClone(outputs);
      }
    };
    // Shop secrets live in separate write-only profiles. Product edits never
    // echo a masked value or an empty configuration back to the server.
    const captureConfiguration = () => {};
    const drawFields = (selector, prefix, fields, readonly, codeDefined) => {
      const container = $(selector);
      if (codeDefined) {
        const stock = $("#p-mode").value === "stock";
        container.innerHTML = `<p class="caption" data-schema-source="${stock ? "stock" : "processor"}">${stock ? "一卡一文本自动交付，无需配置字段。" : "由商品处理器代码定义，只读。"}</p>${fields.length ? fields.map((definition) => `<div class="parameter"><h3>${escape(localized(definition.label, ctx.lang))}</h3><p class="mono">${escape(definition.key)} · ${escape(definition.type)} · ${definition.required ? "必填" : "选填"}</p>${tutorial(definition, ctx.lang, prefix === "f" ? "填写教程" : "结果说明")}</div>`).join("") : '<p class="caption">无需额外填写信息。</p>'}`;
        return;
      }
      container.innerHTML = fields
        .map(
          (definition, index) =>
            `<div class="parameter"><h3>${prefix === "f" ? "参数" : "输出字段"} ${index + 1}${readonly ? "" : `<button type="button" class="danger" data-remove-${prefix}="${index}">删除</button>`}</h3><div class="grid">${field(prefix + "-key-" + index, "字段代码名", definition.key, "text", readonly ? "disabled" : "")}${select(prefix + "-type-" + index, "字段类型", inputTypes, definition.type, readonly ? "disabled" : "")}</div>${textarea(prefix + "-label-" + index, "显示名称（语言 → 文本 JSON）", JSON.stringify(definition.label, null, 2), readonly ? "disabled" : "")}${textarea(prefix + "-description-" + index, "Markdown 教程（语言 → 文本 JSON）", JSON.stringify(definition.description || {}, null, 2), readonly ? "disabled" : "")}<div id="${prefix}-options-section-${index}">${textarea(prefix + "-options-" + index, "下拉选项（JSON 数组）", JSON.stringify(definition.options || [{ value: "basic", label: { "zh-CN": "基础版", en: "Basic" } }], null, 2), readonly ? "disabled" : "")}<p class="caption">value 是传给处理程序的固定值；label 是顾客看到的名称。例如 [{"value":"basic","label":{"zh-CN":"基础版"}}]。</p></div><div id="${prefix}-images-section-${index}">${field(prefix + "-max-items-" + index, "最多图片数量", definition.max_items || 10, "number", `min="1" max="20" step="1" ${readonly ? "disabled" : ""}`)}<p class="caption">支持 PNG、JPEG、WebP；每张图片分别受上传大小限制。</p></div><div class="checks"><label><input id="${prefix}-required-${index}" type="checkbox" ${definition.required ? "checked" : ""} ${readonly ? "disabled" : ""}>必填</label><label><input id="${prefix}-collapsed-${index}" type="checkbox" ${definition.collapsed ? "checked" : ""} ${readonly ? "disabled" : ""}>默认折叠教程</label></div></div>`,
        )
        .join("");
      fields.forEach((definition, index) => {
        const type = $("#" + prefix + "-type-" + index);
        const updateType = () => {
          $("#" + prefix + "-options-section-" + index).hidden = type.value !== "select";
          $("#" + prefix + "-images-section-" + index).hidden = type.value !== "images";
          $("#" + prefix + "-options-" + index).disabled = readonly || type.value !== "select";
          $("#" + prefix + "-max-items-" + index).disabled = readonly || type.value !== "images";
        };
        type.addEventListener("change", updateType);
        updateType();
      });
      all(`[data-remove-${prefix}]`).forEach((node) =>
        node.addEventListener("click", () =>
          perform(() => {
            captureCustom();
            const index = Number(node.getAttribute(`data-remove-${prefix}`));
            if (prefix === "f") {
              parameters.splice(index, 1);
              customParameters = structuredClone(parameters);
            } else {
              outputs.splice(index, 1);
              customOutputs = structuredClone(outputs);
            }
            drawSchemas();
          }, node),
        ),
      );
    };
    function drawSchemas() {
      const codeDefined = ["script", "stock"].includes($("#p-mode").value);
      const service = $("#p-delivery").value === "service";
      $("#add-param").hidden = codeDefined;
      $("#add-output").hidden = codeDefined || service || !ctx.canConfigure;
      $("#outputs-section").hidden = service;
      drawFields("#parameters", "f", parameters, false, codeDefined);
      drawFields(
        "#outputs",
        "o",
        service ? [] : outputs,
        !ctx.canConfigure,
        codeDefined,
      );
    }
    function drawConfiguration() {
      const spec = catalog.find((item) => item.id === processorId);
      displayedProcessorId = spec?.id || "";
      $("#processor-description").textContent = spec
        ? localized(spec.description, ctx.lang)
        : "请选择一个商品处理器。";
      if (!owner) {
        $("#processor-configuration").innerHTML = '<p class="caption">商品处理器配置由店主单独管理，配置已隐藏。</p>';
        return;
      }
      if (!spec) { $("#processor-configuration").innerHTML = ""; return; }
      if (!product.id) {
        $("#processor-configuration").innerHTML = '<p class="caption">先保存商品，再选择店铺的处理器配置。变量、账号和密钥在“处理器配置”中单独设置。</p>';
        return;
      }
      if (!profilesRequested) {
        profilesRequested = true;
        const load = ++bindingLoad;
        const targetShop = product.shop_id || ctx.shopId;
        const query = ctx.superadmin && targetShop ? "?" + new URLSearchParams({ shop_id: targetShop }) : "";
        Promise.all([ctx.api("/admin/processor-profiles" + query), ctx.api("/admin/processor-profiles/bindings/" + encodeURIComponent(product.id))])
          .then(([values, binding]) => {
            if (!active() || load !== bindingLoad) return;
            profiles = values;
            profileBinding = binding;
            drawConfiguration();
          }).catch((error) => { if (active() && load === bindingLoad) report(error); });
      }
      const matching = profiles.filter((profile) => !profile.disabled && profile.processor_id === processorId && (!product.shop_id || profile.shop_id === product.shop_id));
      const current = profileBinding?.profile;
      const boundToProduct = current?.processor_id === spec.id && current.shop_id === (product.shop_id || ctx.shopId);
      const visibleConfiguration = boundToProduct && current.configuration && typeof current.configuration === "object" && !Array.isArray(current.configuration) ? current.configuration : {};
      const configurationFields = new Map();
      for (const definition of spec.configuration || []) {
        if (configurationFields.has(definition.key)) configurationFields.set(definition.key, { ...configurationFields.get(definition.key), secret: true });
        else configurationFields.set(definition.key, definition);
      }
      const preview = [...configurationFields.values()].filter((definition) => definition.secret === false && Object.hasOwn(visibleConfiguration, definition.key) && typeof visibleConfiguration[definition.key] === "string");
      const previewHTML = preview.length ? `<div class="form-divider processor-bound-preview"><h4>${ctx.lang === "en" ? "Plain text in the bound configuration version" : "当前绑定版本的普通文本"}</h4>${preview.map((definition, index) => {
        const label = localized(definition.label, ctx.lang), value = visibleConfiguration[definition.key];
        return definition.type === "textarea" ? textarea("profile-preview-" + index, label, value, 'readonly autocomplete="off" spellcheck="false" rows="5"') : field("profile-preview-" + index, label, value, "text", 'readonly autocomplete="off"');
      }).join("")}</div>` : "";
      const workflow = boundToProduct && current.workflow && typeof current.workflow === "object" && !Array.isArray(current.workflow) ? current.workflow : null;
      const variableEntries = workflow?.variables && typeof workflow.variables === "object" && !Array.isArray(workflow.variables) ? Object.entries(workflow.variables).filter(([name, value]) => /^[A-Z][A-Z0-9_]{0,63}$/.test(name) && typeof value === "string").slice(0, 64) : [];
      const secretNames = Array.isArray(workflow?.configured_secret_names) ? [...new Set(workflow.configured_secret_names.filter((name) => typeof name === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(name)))].slice(0, 64) : [];
      const runtimeLabels = [["timeout_seconds", "最长运行时间（秒）", "Timeout (seconds)", 10, 120], ["memory_mb", "内存上限（MB）", "Memory (MB)", 64, 512], ["cpu_seconds", "CPU 时间（秒）", "CPU time (seconds)", 1, 120], ["max_output_bytes", "输出上限（字节）", "Output limit (bytes)", 65536, 1000000]];
      const runtimeEntries = runtimeLabels.filter(([key, zh, en, low, high]) => Number.isSafeInteger(workflow?.runtime?.[key]) && workflow.runtime[key] >= low && workflow.runtime[key] <= high);
      const workflowHTML = workflow ? `<div class="form-divider processor-workflow-preview" style="overflow-wrap:anywhere"><h4>${ctx.lang === "en" ? "Variables and runtime in the bound version" : "当前绑定版本的变量与运行环境"}</h4>${variableEntries.map(([name, value], index) => textarea("workflow-variable-" + index, name, value, 'readonly autocomplete="off" spellcheck="false" rows="3"')).join("")}${secretNames.length ? `<p class="caption">${ctx.lang === "en" ? "Secrets (values hidden)" : "密钥（不显示值）"}</p><ul>${secretNames.map((name) => `<li>${escape(name)} · ${ctx.lang === "en" ? "Configured" : "已设置"}</li>`).join("")}</ul>` : ""}${runtimeEntries.length ? `<dl>${runtimeEntries.map(([key, zh, en]) => `<dt>${escape(ctx.lang === "en" ? en : zh)}</dt><dd>${escape(workflow.runtime[key])}</dd>`).join("")}</dl>` : ""}<p class="caption">${ctx.lang === "en" ? "Variables are provided separately to the processor. They are never automatically inserted into delivery text." : "变量单独传给处理器，不会自动插入交付文本。"}</p></div>` : "";
      const editHint = current ? `<p class="caption">${ctx.lang === "en" ? "Preview only. Edit in Processor configurations, save, then return here and bind the new version. Existing codes continue using their original version." : "这里只预览。请在「处理器配置」中编辑模板与变量，保存后回到此商品选择新版本；已发行卡密继续使用原版本。"}</p>` : "";
      $("#processor-configuration").innerHTML = `<p class="caption">普通文本可预览，密码和密钥不会显示。绑定只影响之后发行的卡密，已发行卡密保留原绑定。</p>${current ? `<p>当前配置：${escape(current.name)} · 绑定版本 ${escape(current.bound_revision || current.revision)}</p>` : '<p class="caption">尚未绑定处理器配置。</p>'}${previewHTML}${workflowHTML}${editHint}${select("p-profile", "店铺处理器配置", [["", "请选择配置"], ...matching.map((profile) => [profile.id, profile.name + " · 版本 " + profile.revision])], current?.id || "")}<div class="actions"><button id="bind-profile" type="button" class="secondary">绑定所选配置</button>${current ? '<button id="unbind-profile" type="button" class="danger">解除未来卡密的配置绑定</button>' : ""}</div>`;
      on("#bind-profile", "click", async () => {
        const chosen = $("#p-profile").value;
        if (!matching.some((profile) => profile.id === chosen)) throw new Error("请选择属于当前店铺的处理器配置");
        const load = ++bindingLoad;
        const result = await ctx.api("/admin/processor-profiles/bindings/" + encodeURIComponent(product.id), { profile_id: chosen }, "PUT");
        if (!active() || load !== bindingLoad) return;
        profileBinding = result;
        drawConfiguration();
        ctx.notify("处理器配置已绑定，仅用于之后发行的卡密");
      });
      on("#unbind-profile", "click", async () => {
        const load = ++bindingLoad;
        await ctx.api("/admin/processor-profiles/bindings/" + encodeURIComponent(product.id), null, "DELETE");
        if (!active() || load !== bindingLoad) return;
        profileBinding = null;
        drawConfiguration();
      });
    }
    function syncMode() {
      const mode = $("#p-mode").value;
      const codeDefined = mode === "script";
      const stock = mode === "stock";
      $("#queue-help").hidden = mode !== "manual";
      $("#stock-help").hidden = !stock;
      $("#product-progress-section").hidden = stock;
      $("#product-task-flow").hidden = stock;
      $("#webhook-settings").hidden = mode !== "webhook";
      $("#processor-settings").hidden = !codeDefined;
      $("#p-delivery").disabled = codeDefined || stock || !ctx.canConfigure;
      $("#p-view").disabled = !ctx.canConfigure;
      if (codeDefined) {
        const spec = catalog.find((item) => item.id === processorId);
        if (spec) {
          $("#p-delivery").value = spec.delivery;
          parameters = structuredClone(spec.parameters);
          outputs = structuredClone(spec.outputs);
        }
      }
      if (stock) {
        $("#p-delivery").value = "content";
        parameters = [];
        outputs = [defaultOutput()];
      }
      $("#p-view").closest(".field").hidden =
        $("#p-delivery").value === "service";
      previousMode = mode;
      previousDelivery = $("#p-delivery").value;
      drawSchemas();
    }
    on("#cancel", "click", () => render(ctx));
    on("#export-saved-product", "click", () => exportProduct(product.id));
    on("#add-progress-step", "click", () => {
      captureProgressSteps(false);
      if (progressSteps.length >= 30)
        throw new Error("每个商品最多支持 30 个处理步骤");
      const number = progressSteps.length + 1;
      progressSteps.push({
        id: newItemId("step"),
        label: { "zh-CN": "步骤 " + number, en: "Step " + number },
      });
      drawProgressSteps();
    });
    on("#add-variant", "click", () => {
      captureVariants();
      if (variants.length >= 100)
        throw new Error("每个商品最多支持 100 个规格");
      variants.push({
        ...defaultVariant(),
        id: newItemId("sku"),
        name: "基础版",
      });
      drawVariants();
    });
    on("#add-param", "click", () => {
      captureCustom();
      parameters.push({
        key: "field_" + (parameters.length + 1),
        label: { "zh-CN": "参数名称", en: "Parameter" },
        description: {},
        type: "text",
        required: true,
        collapsed: true,
      });
      customParameters = structuredClone(parameters);
      drawSchemas();
    });
    on("#add-output", "click", () => {
      if (!ctx.canConfigure) return;
      captureCustom();
      outputs.push({
        ...defaultOutput(),
        key: "result_" + (outputs.length + 1),
        label: { "zh-CN": "结果名称", en: "Result" },
        type: "text",
      });
      customOutputs = structuredClone(outputs);
      drawSchemas();
    });
    on("#p-mode", "change", () => {
      try {
        captureCustom();
        captureConfiguration();
      } catch (error) {
        $("#p-mode").value = previousMode;
        throw error;
      }
      if ($("#p-mode").value !== "script") {
        parameters = structuredClone(customParameters);
        outputs = structuredClone(customOutputs);
      }
      syncMode();
    });
    on("#p-delivery", "change", () => {
      try {
        captureCustom();
      } catch (error) {
        $("#p-delivery").value = previousDelivery;
        throw error;
      }
      if ($("#p-delivery").value === "content" && !outputs.length)
        outputs = [defaultOutput()];
      syncMode();
    });
    on("#p-processor", "change", () => {
      captureConfiguration();
      processorId = $("#p-processor").value;
      drawConfiguration();
      syncMode();
    });
    on("#product-form", "submit", async () => {
      const button = $("#save-product");
      if (button.disabled) return;
      button.disabled = true;
      try {
        captureVariants();
        captureProgressSteps();
        captureCustom();
        captureConfiguration();
        const mode = $("#p-mode").value;
        if (
          mode === "script" &&
          !catalog.some((item) => item.id === processorId)
        )
          throw new Error("请选择可用的商品处理器");
        const body = {
          name: $("#p-name").value,
          description: $("#p-description").value,
          logo: $("#p-logo").value,
          image: $("#p-image").value,
          public: $("#p-public").checked,
          mode,
          delivery: $("#p-delivery").value,
          view_policy: $("#p-view").value,
          allow_retry: $("#p-retry").checked,
          max_attempts: Number($("#p-attempts").value),
          parameters,
          outputs: $("#p-delivery").value === "service" ? [] : outputs,
          webhook_url: $("#p-url").value,
          webhook_secret: $("#p-secret").value,
          script: "",
          processor_id: mode === "script" ? processorId : "",
          variants,
          progress_steps: mode === "stock" ? [] : progressSteps,
          task_flow: mode === "stock" ? null : taskFlowEditor ? taskFlowEditor.getValue() : product.task_flow || null,
          support_email: $("#p-support-email").value.trim(),
        };
        // Configuration is not part of generic product editing. The existing
        // processor ID remains unchanged for product-scoped managers.
        if (!owner) body.processor_id = product.processor_id || "";
        const result = await ctx.api(
          ctx.role === "staff"
            ? "/manage/product"
            : "/admin/products" + (product.id ? "/" + product.id : ""),
          body,
          product.id ? "PUT" : "POST",
        );
        // A late save belongs to its original view and tenant.
        if (!active()) return;
        const updated =
          ctx.role === "staff"
            ? [result]
            : product.id
              ? ctx.products.map((item) =>
                  item.id === product.id ? result : item,
                )
              : [...ctx.products, result];
        ctx.products = updated;
        ctx.onSaved(updated);
        if (!active()) return;
        await render(ctx);
        if (ctx.isCurrent()) ctx.notify("商品已保存");
      } finally {
        if (active() && button.isConnected) button.disabled = false;
      }
    });
    syncMode();
    ctx.refreshTools();
    try {
      const response = await ctx.api(
        ctx.role === "staff" ? "/manage/processors" : "/admin/processors",
      );
      if (!active()) return;
      catalog = Array.isArray(response) ? response : response.processors || [];
      $("#p-processor").innerHTML =
        `<option value="">请选择商品处理器</option>${catalog.map((spec) => `<option value="${escape(spec.id)}">${escape(localized(spec.name, ctx.lang))}</option>`).join("")}`;
      if (processorId && !catalog.some((spec) => spec.id === processorId)) {
        $("#p-processor").innerHTML +=
          `<option value="${escape(processorId)}">${escape(processorId)}（当前版本不可用）</option>`;
      }
      $("#p-processor").value = processorId;
      drawConfiguration();
      // Queue/Webhook fields may have been edited while this request was pending.
      // They do not depend on the catalog, so retain their live DOM values.
      if ($("#p-mode").value === "script") syncMode();
    } catch (error) {
      report(error);
      if (active())
        $("#p-processor").innerHTML =
          '<option value="">处理器列表加载失败</option>';
    }
  }

  window.ExtoreProducts = { render, edit };
})();
