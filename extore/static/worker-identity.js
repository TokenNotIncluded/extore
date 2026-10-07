"use strict";

(() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const types = { dots: "dots", "grok-bot": "grok-bot", other: "other" };
  const kindNames = { human: ["人工", "Human"], cli: ["CLI", "CLI"], automatic: ["商品处理器", "Product processor"], merchant: ["店主", "Merchant"], unknown: ["处理人员", "Worker"] };
  function validType(value) {
    return value === undefined || value === null || (typeof value === "string" && value.trim().length >= 1 && value.length <= 64 && !/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value));
  }
  function markup(worker, options = {}) {
    if (!worker || typeof worker.name !== "string" || worker.name.length > 256 || !Object.hasOwn(kindNames, worker.kind) || !validType(worker.agent_type)) return "";
    const en = options.language === "en", tr = (cn, english) => en ? english : cn;
    const declared = typeof worker.agent_type === "string" ? worker.agent_type.trim() : "";
    const key = declared.toLowerCase().replace(/[\s_]+/g, "-");
    const avatar = worker.kind === "automatic" ? "processor" : ["human", "merchant"].includes(worker.kind) ? "human" : Object.hasOwn(types, key) ? types[key] : "other";
    const note = declared && worker.kind !== "automatic"
      ? tr(`自报类型：${declared} · 未验证`, `Declared type: ${declared} · unverified`)
      : worker.kind === "cli" ? tr("未声明类型 · 名称未经验证", "Type not declared · name unverified") : tr(...kindNames[worker.kind]);
    return `<span class="worker-identity${options.compact ? " worker-identity-compact" : ""}"><img class="worker-avatar" src="/static/worker-avatars/${avatar}.webp" alt="" width="48" height="48" loading="lazy" decoding="async"><span class="worker-identity-text"><strong>${esc(worker.name || tr("未命名处理人员", "Unnamed worker"))}</strong><span class="caption">${esc(note)}</span></span></span>`;
  }
  window.ExtoreWorkerIdentity = Object.freeze({ validType, markup });
})();
