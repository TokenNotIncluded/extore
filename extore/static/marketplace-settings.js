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
  const templates = [["card-key","激活卡密"], ["text","文本"], ["custom-text","自定义文本"], ["fixed-content","固定交付内容"], ["redemption-code","兑换码"], ["license-key","许可证密钥"], ["download-link","下载链接"], ["account-details","账号资料"]];
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
  function variant(value, index) {
    const config = value.marketplace || {}, price = config.price_quota == null ? "" : String(config.price_quota);
    return `<details><summary>此规格的商城销售配置</summary>${input("mp-v-price-"+index,"商城售价（Credits，500000 = 1 美元）",price,"number")}${input("mp-v-template-"+index,"商城交付方式",config.template || "card-key",templates)}${input("mp-v-content-"+index,"固定交付内容（仅固定内容方式使用）",config.fixed_content || "","textarea")}</details>`;
  }
  function captureVariant(root, index, source) {
    const price = root.querySelector("#mp-v-price-"+index).value.trim();
    const parsed = price ? Number(price) : null;
    if (parsed !== null && (!Number.isSafeInteger(parsed) || parsed <= 0)) throw new Error("商城售价须为正整数 Credits");
    return {...source,price_quota:parsed,template:root.querySelector("#mp-v-template-"+index).value,fixed_content:root.querySelector("#mp-v-content-"+index).value || null};
  }
  window.ExtoreMarketplaceSettings = Object.freeze({product,bind,captureProduct,variant,captureVariant});
})();
