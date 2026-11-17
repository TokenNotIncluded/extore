"use strict";
(() => {
  const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const productFields = [
    ["title", "商城商品名称（留空沿用商品名称）"], ["description", "商城商品说明（留空沿用商品说明）", "textarea"],
    ["image_urls", "商城图片（每行一个地址；留空沿用商品图片）", "lines"], ["contact", "商城联系方式（留空沿用商家邮箱）"],
    ["category_id", "商城分类编号"], ["visibility", "商城可见范围", ["", "public", "private", "unlisted"]],
    ["delivery_strategy", "库存发货顺序", ["sequential", "random"]],
    ["max_quantity_per_order", "每笔订单最多购买数量（留空不限）", "number"], ["max_quantity_per_buyer", "每位买家最多购买数量（留空不限）", "number"],
    ["sale_limit", "商品总销售数量上限（留空不限）", "number"],
    ["test_mode", "启用商城测试模式", "boolean"], ["purchase_login_required", "购买必须登录", "boolean", true],
    ["pickup_login_required", "提货必须登录", "boolean", true], ["pickup_code_required", "提货需要提货码", "boolean"],
    ["email_pickup_link", "发送提货邮件", "boolean"],
  ];
  const payments = [["balance", "余额"], ["platform:waffo_pancake", "平台 Waffo"], ["platform:linuxdo", "Linux DO"], ["external:epay", "自有易支付"], ["external:waffo_pancake", "自有 Waffo"]];
  const templates = [
    ["card-key", "激活卡密", "Activation keys"],
    ["text", "文本", "Text items"],
    ["custom-text", "自定义文本", "Custom text"],
    ["fixed-content", "固定交付内容", "Fixed content"],
    ["redemption-code", "兑换码", "Redemption codes"],
    ["license-key", "许可证密钥", "License keys"],
    ["download-link", "下载链接", "Download links"],
    ["account-details", "账号资料", "Account details"],
  ];
  const labels = {"public":"公开", "private":"私密", "unlisted":"不公开列出", "sequential":"顺序发货", "random":"随机发货", "":"沿用商品可见范围"};
  const input = (id, label, value, kind = "text") => `<div class="field"><label for="${id}">${escape(label)}</label>${Array.isArray(kind) ? `<select id="${id}">${kind.map((v) => { const [key, title] = Array.isArray(v) ? v : [v, labels[v] || v]; return `<option value="${escape(key)}" ${value === key ? "selected" : ""}>${escape(title)}</option>`; }).join("")}</select>` : kind === "boolean" ? `<input id="${id}" type="checkbox" ${value ? "checked" : ""}>` : kind === "textarea" || kind === "lines" ? `<textarea id="${id}" rows="3">${escape(Array.isArray(value) ? value.join("\n") : value)}</textarea>` : `<input id="${id}" type="${kind}" ${kind === "number" ? 'min="1" step="1"' : ""} value="${escape(value)}">`}</div>`;
  function product(product) {
    const config = product.marketplace || {};
    return `<details class="commerce-paper"><summary>商城销售配置</summary><p class="caption">这里是商城商品配置的唯一来源。同步会更新原商品及全部规格，已售订单不受影响。1 美元 = 500000 Credits。卡密数量是兑换状态统计，补货需单独授权。</p>${productFields.map(([key,label,kind,def]) => input("mp-"+key,label,config[key] ?? def ?? "",kind)).join("")}<fieldset><legend>商城支付方式</legend>${payments.map(([key,label],i) => input("mp-payment-"+i,label,(config.payment_methods || ["balance"]).includes(key),"boolean")).join("")}</fieldset><div id="mp-links">${(config.links || []).map((link,i) => `<div data-mp-link>${input("mp-link-title-"+i,"链接名称",link.title)}${input("mp-link-url-"+i,"链接地址",link.url,"url")}${input("mp-link-description-"+i,"链接说明",link.description)}</div>`).join("")}</div><button type="button" id="mp-add-link" class="secondary">添加商城链接</button></details>`;
  }
  function bind(root) {
    root.querySelector("#mp-add-link")?.addEventListener("click", () => {
      const target = root.querySelector("#mp-links"), i = target.querySelectorAll("[data-mp-link]").length;
      target.insertAdjacentHTML("beforeend", `<div data-mp-link>${input("mp-link-title-"+i,"链接名称","")}${input("mp-link-url-"+i,"链接地址","","url")}${input("mp-link-description-"+i,"链接说明","")}</div>`);
    });
  }
  function captureProduct(root, source) {
    const config = {...source};
    for (const [key,,kind] of productFields) {
      const control = root.querySelector("#mp-"+key), value = control.value.trim();
      config[key] = kind === "boolean" ? control.checked : kind === "number" ? (value ? Number(value) : null) : kind === "lines" ? (value ? value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean) : null) : value || null;
    }
    config.delivery_strategy ||= "sequential";
    config.payment_methods = payments.flatMap(([key],i) => root.querySelector("#mp-payment-"+i).checked ? [key] : []);
    config.links = [...root.querySelectorAll("[data-mp-link]")].flatMap((_,i) => {
      const url = root.querySelector("#mp-link-url-"+i).value.trim();
      return url ? [{title:root.querySelector("#mp-link-title-"+i).value.trim(),url,description:root.querySelector("#mp-link-description-"+i).value.trim()}] : [];
    });
    return config;
  }
  function variant(value, index, lang) {
    const en = lang === "en", config = value.marketplace || {};
    const tr = (zh, english) => en ? english : zh;
    return `<details class="marketplace-variant"><summary>${tr("商城规格配置", "Marketplace SKU settings")}</summary>
      <p class="caption">${tr("模板与 api.lmm.best 对齐；能否导入和发货仍取决于目标商城能力，不会自动发行或补货。固定正文仅配置发货人员可见，不进入公开商品或普通商品导出。", "Templates follow api.lmm.best; import and fulfillment depend on the target server. No automatic issuance or restocking. Fixed content is private to fulfillment configurators and excluded from public products and ordinary exports.")}</p>
      <div class="field"><label for="mp-template-${index}">${tr("商城交付模板", "Marketplace delivery template")}</label><select id="mp-template-${index}">${templates.map(([key, zh, english]) => `<option value="${key}" ${key === (config.template || "card-key") ? "selected" : ""}>${escape(en ? english : zh)}</option>`).join("")}</select></div>
      <div class="field"><label for="mp-price-${index}">${tr("售价（Credits，可留空；最低 500000，不从参考价自动换算）", "Price (Credits, optional; minimum 500000, never converted from reference price)")}</label><input id="mp-price-${index}" type="text" inputmode="numeric" value="${escape(config.price_quota)}"></div>
      <div class="field"><label for="mp-content-${index}">${tr("固定交付正文（仅固定内容模板，最多 16000 UTF-8 字节）", "Fixed delivery body (fixed-content only; up to 16000 UTF-8 bytes)")}</label><textarea id="mp-content-${index}" rows="3">${escape(config.fixed_content)}</textarea></div>
    </details>`;
  }
  function captureVariant(root, index) {
    const template = root.querySelector(`#mp-template-${index}`).value;
    const price = root.querySelector(`#mp-price-${index}`).value.trim();
    if (price && (!/^\d+$/.test(price) || !Number.isSafeInteger(Number(price)) || Number(price) < 500000))
      throw new Error("商城售价须为 500000 至 9007199254740991 的整数 Credits / Invalid Credits price");
    const content = root.querySelector(`#mp-content-${index}`).value;
    if (template === "fixed-content" && (!content.trim() || new TextEncoder().encode(content).length > 16000))
      throw new Error("固定内容须为 1 至 16000 UTF-8 字节 / Invalid fixed content");
    if (template !== "fixed-content" && content)
      throw new Error("切换模板前请清空固定正文，避免误丢失 / Clear fixed content before changing template");
    return {template, price_quota: price ? Number(price) : null, fixed_content: template === "fixed-content" ? content : null};
  }
  window.ExtoreMarketplaceSettings = Object.freeze({product, bind, captureProduct, variant, captureVariant});
})();
