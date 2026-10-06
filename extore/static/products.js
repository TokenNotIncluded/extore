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
  const inputTypes = [
    ["text", "文本"],
    ["email", "邮箱"],
    ["url", "链接"],
    ["textarea", "多行文本"],
    ["number", "数字"],
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

  async function render(ctx, createdLink = null) {
    if (!ctx.isCurrent()) return;
    const { active, $, all, on, report } = begin(ctx, "list");
    const owner = ctx.role === "admin";
    const products = ctx.products;
    ctx.workspace.innerHTML = `<div class="section-head"><h2>商品</h2>${owner ? '<button id="new-product">新建商品</button>' : ""}</div>
      ${owner ? `<div class="form-divider"><div class="grid">${select("quick-template", "快速新建模板", [["random", "随机选择模板"]], "random")}<div class="field"><label for="quick-product">生成私有草稿与 AI 配置链接</label><button id="quick-product" class="secondary" disabled>随机快速新建</button></div></div><p class="caption">先创建一个私有商品，再把仅能配置这个商品的链接交给 AI 完善。配置链接有效期为 7 天。复制已有商品后，需重新填写私密发货配置。</p></div>` : ""}
      ${createdLink ? `<div class="parameter" id="quick-created"><h3>商品已创建 · ${escape(createdLink.productName)}</h3><p class="caption">这个链接只允许编辑当前商品与发货配置。请复制保存；离开后完整链接不再显示。</p>${field("quick-management-link", "AI 商品配置链接", createdLink.url, "text", "readonly")}<div class="toolbar"><button id="copy-quick-link" class="secondary">复制配置链接</button><button id="edit-quick-product" class="secondary">继续配置商品</button></div></div>` : ""}
      ${products.length ? `<div class="product-list">${products.map((product) => `<article class="product-row">${product.logo ? `<img src="${escape(product.logo)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="product-icon">${icon}</div>`}<div class="product-info"><h3>${escape(product.name)}</h3><p>${product.public ? "公开展示" : "仅持卡可见"} · ${escape({ manual: "队列", webhook: "外部 Webhook", script: "官方处理器" }[product.mode] || product.mode)} · ${product.delivery === "service" ? "服务状态" : "内容交付"}</p><div class="mono muted">${escape(product.id)}</div></div><button class="secondary" data-edit="${escape(product.id)}">配置</button></article>`).join("")}</div>` : '<div class="empty">还没有商品。先创建商品，再生成卡密。</div>'}
      <div id="error" class="error" role="alert"></div>`;
    on("#new-product", "click", () => edit(ctx));
    all("[data-edit]").forEach((node) =>
      node.addEventListener("click", () => {
        if (active())
          edit(
            ctx,
            products.find((product) => product.id === node.dataset.edit),
          );
      }),
    );
    if (createdLink) {
      on("#copy-quick-link", "click", async () => {
        const input = $("#quick-management-link");
        if (navigator.clipboard?.writeText) {
          try {
            await navigator.clipboard.writeText(createdLink.url);
            if (active()) ctx.notify("配置链接已复制");
            return;
          } catch {
            // A browser clipboard denial still allows manual copying.
          }
        }
        if (!active()) return;
        input.focus();
        input.select();
        ctx.notify("请复制已选中的配置链接");
      });
      on("#edit-quick-product", "click", () =>
        edit(
          ctx,
          products.find((product) => product.id === createdLink.productId),
        ),
      );
    }
    ctx.refreshTools();
    if (!owner) return;
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
      `<option value="random">随机选择模板</option>${templates.map((template) => `<option value="${escape(template.id)}">${escape(localized(template.name, ctx.lang))}</option>`).join("")}${products.map((product) => `<option value="existing_product:${escape(product.id)}">复制：${escape(product.name)}</option>`).join("")}`;
    $("#quick-product").disabled = !templates.length && !products.length;
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
    const { active, $, all, perform, on, report } = begin(ctx, "editor");
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
    const disabled = ctx.canConfigure ? "" : "disabled";
    ctx.workspace.innerHTML = `<div class="section-head"><h2>${product.id ? "配置商品" : "新建商品"}</h2><button id="cancel" class="secondary">返回商品</button></div><form id="product-form">
      <div class="grid">${field("p-name", "商品名称", product.name)}${select(
        "p-mode",
        "处理方式",
        [
          ["manual", "队列"],
          ["webhook", "外部 Webhook"],
          ["script", "官方处理器"],
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
      <div class="checks"><label><input id="p-public" type="checkbox" ${product.public ? "checked" : ""}>公开展示商品</label><label><input id="p-retry" type="checkbox" ${product.allow_retry ? "checked" : ""} ${disabled}>允许明确失败后重试</label></div>
      ${field("p-attempts", "最多尝试次数", product.max_attempts, "number", `${disabled} min="1" max="20"`)}
      <div class="form-divider" id="delivery-connection"><h3>发货对接</h3>
        <p id="queue-help" class="caption">队列任务可以由人员或 AI 领取处理。下方定义顾客填写的信息，以及完成任务时必须提交的结果。</p>
        <div id="webhook-settings"><p class="caption">将任务交给外部平台处理。接收地址须为 HTTPS 公网地址，使用签名密钥验证任务与回调。</p><div class="grid">${field("p-url", "Webhook 接收地址", product.webhook_url || "", "url", disabled)}${field("p-secret", "Webhook 签名密钥（至少 32 字符）", product.webhook_secret || "", "password", `${disabled} autocomplete="new-password"`)}</div></div>
        <div id="processor-settings">${select("p-processor", "官方商品处理器", [["", "正在加载处理器…"]], "", disabled)}<p id="processor-description" class="caption"></p><div id="processor-configuration"></div><p class="caption">顾客填写项与交付结果由处理器代码定义。这里只能选择官方预设并填写它声明的配置。</p></div>
      </div>
      <div class="form-divider"><div class="section-head"><h3>顾客填写的信息</h3><button type="button" id="add-param" class="secondary">添加参数</button></div><div id="parameters"></div></div>
      <div class="form-divider" id="outputs-section"><div class="section-head"><h3>任务完成时提交的结果</h3><button type="button" id="add-output" class="secondary" ${disabled}>添加输出字段</button></div><p class="caption">结果只在顾客主动领取时显示。人员、AI 或外部平台提交完成结果时，都须符合这些定义。</p><div id="outputs"></div></div>
      <p class="caption">发行卡密后，处理方式与输入输出结构不能更换。需要更换时，请创建另一个商品。</p><button type="submit" id="save-product" class="full">保存商品</button><div id="error" class="error" role="alert"></div>
    </form>`;

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
    const captureFields = (prefix, fields) =>
      fields.map((definition, index) => ({
        ...definition,
        key: $("#" + prefix + "-key-" + index).value,
        label: parseObject(prefix + "-label-" + index, "显示名称"),
        description: parseObject(prefix + "-description-" + index, "填写教程"),
        type: $("#" + prefix + "-type-" + index).value,
        required: $("#" + prefix + "-required-" + index).checked,
        collapsed: $("#" + prefix + "-collapsed-" + index).checked,
      }));
    const captureCustom = () => {
      if (previousMode !== "script") {
        parameters = captureFields("f", parameters);
        if (previousDelivery === "content")
          outputs = captureFields("o", outputs);
        customParameters = structuredClone(parameters);
        customOutputs = structuredClone(outputs);
      }
    };
    const captureConfiguration = () => {
      if (!displayedProcessorId || !ctx.canConfigure) return;
      const spec = catalog.find((item) => item.id === displayedProcessorId);
      if (!spec) return;
      processorConfig = Object.fromEntries(
        spec.configuration.map((definition, index) => [
          definition.key,
          $("#pc-value-" + index).value,
        ]),
      );
      configByProcessor.set(displayedProcessorId, processorConfig);
    };
    const drawFields = (selector, prefix, fields, readonly, codeDefined) => {
      const container = $(selector);
      if (codeDefined) {
        container.innerHTML = `<p class="caption" data-schema-source="processor">由官方处理器代码定义，只读。</p>${fields.length ? fields.map((definition) => `<div class="parameter"><h3>${escape(localized(definition.label, ctx.lang))}</h3><p class="mono">${escape(definition.key)} · ${escape(definition.type)} · ${definition.required ? "必填" : "选填"}</p>${tutorial(definition, ctx.lang, prefix === "f" ? "填写教程" : "结果说明")}</div>`).join("") : '<p class="caption">无需额外填写信息。</p>'}`;
        return;
      }
      container.innerHTML = fields
        .map(
          (definition, index) =>
            `<div class="parameter"><h3>${prefix === "f" ? "参数" : "输出字段"} ${index + 1}${readonly ? "" : `<button type="button" class="danger" data-remove-${prefix}="${index}">删除</button>`}</h3><div class="grid">${field(prefix + "-key-" + index, "字段代码名", definition.key, "text", readonly ? "disabled" : "")}${select(prefix + "-type-" + index, "字段类型", inputTypes, definition.type, readonly ? "disabled" : "")}</div>${textarea(prefix + "-label-" + index, "显示名称（语言 → 文本 JSON）", JSON.stringify(definition.label, null, 2), readonly ? "disabled" : "")}${textarea(prefix + "-description-" + index, "Markdown 教程（语言 → 文本 JSON）", JSON.stringify(definition.description || {}, null, 2), readonly ? "disabled" : "")}<div class="checks"><label><input id="${prefix}-required-${index}" type="checkbox" ${definition.required ? "checked" : ""} ${readonly ? "disabled" : ""}>必填</label><label><input id="${prefix}-collapsed-${index}" type="checkbox" ${definition.collapsed ? "checked" : ""} ${readonly ? "disabled" : ""}>默认折叠教程</label></div></div>`,
        )
        .join("");
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
      const codeDefined = $("#p-mode").value === "script";
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
        : "请选择一个官方处理器。";
      if (!spec) {
        $("#processor-configuration").innerHTML = "";
        return;
      }
      if (!ctx.canConfigure) {
        $("#processor-configuration").innerHTML =
          '<p class="caption">当前链接没有修改发货配置的权限，处理器配置已隐藏。</p>';
        return;
      }
      processorConfig = { ...(configByProcessor.get(processorId) || {}) };
      $("#processor-configuration").innerHTML = spec.configuration
        .map((definition, index) => {
          const value =
            processorConfig[definition.key] ?? definition.default ?? "";
          const label = localized(definition.label, ctx.lang);
          const attributes = `${definition.max_length ? `maxlength="${definition.max_length}"` : ""} ${definition.required ? 'data-required="true"' : ""}`;
          return `${definition.type === "textarea" ? textarea("pc-value-" + index, label, value, attributes) : field("pc-value-" + index, label, value, definition.type === "number" ? "number" : definition.type === "url" ? "url" : "text", attributes)}${tutorial(definition, ctx.lang, "配置说明")}`;
        })
        .join("");
    }
    function syncMode() {
      const mode = $("#p-mode").value;
      const codeDefined = mode === "script";
      $("#queue-help").hidden = mode !== "manual";
      $("#webhook-settings").hidden = mode !== "webhook";
      $("#processor-settings").hidden = !codeDefined;
      $("#p-delivery").disabled = codeDefined || !ctx.canConfigure;
      $("#p-view").disabled = !ctx.canConfigure;
      if (codeDefined) {
        const spec = catalog.find((item) => item.id === processorId);
        if (spec) {
          $("#p-delivery").value = spec.delivery;
          parameters = structuredClone(spec.parameters);
          outputs = structuredClone(spec.outputs);
        }
      }
      $("#p-view").closest(".field").hidden =
        $("#p-delivery").value === "service";
      previousMode = mode;
      previousDelivery = $("#p-delivery").value;
      drawSchemas();
    }
    on("#cancel", "click", () => render(ctx));
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
        captureCustom();
        captureConfiguration();
        const mode = $("#p-mode").value;
        if (
          mode === "script" &&
          !catalog.some((item) => item.id === processorId)
        )
          throw new Error("请选择可用的官方处理器");
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
        };
        if (ctx.canConfigure)
          body.processor_config = mode === "script" ? processorConfig : {};
        const result = await ctx.api(
          ctx.role === "staff"
            ? "/manage/product"
            : "/admin/products" + (product.id ? "/" + product.id : ""),
          body,
          product.id ? "PUT" : "POST",
        );
        // Keep the app's in-memory product list current even after navigation.
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
        `<option value="">请选择官方处理器</option>${catalog.map((spec) => `<option value="${escape(spec.id)}">${escape(localized(spec.name, ctx.lang))}</option>`).join("")}`;
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
