"use strict";
(() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
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
  window.ExtoreMarketplaceSettings = Object.freeze({variant, captureVariant});
})();
