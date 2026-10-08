"use strict";

(() => {
  const permissionNames = new Set([
    "queue.view", "queue.process", "queue.retry", "product.edit",
    "fulfillment.configure", "cards.manage", "events.manage", "links.delegate",
    "queue.monitor",
  ]);
  function origin(value) {
    const url = new URL(value);
    if (url.username || url.password || !(url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))))
      throw new Error("CLI 授权仅支持 HTTPS，开发环境可使用本机 HTTP。");
    return url.origin;
  }
  function identifier(value) {
    if (value === undefined || value === null || value === "") return "";
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(value))
      throw new Error("提示词需要有效的店铺、商品、配置或处理器 ID。");
    return value;
  }
  function sentence(options, task) {
    const url = (origin(options.origin) + "/AGENTS.md").replace(/[\s\\`()<>]/g, (value) => "%" + value.charCodeAt(0).toString(16).toUpperCase());
    return options.language === "en"
      ? `Use Extore CLI to ${task}, following the [general rules](${url}).`
      : `请按[通用规则](${url})使用 Extore CLI ${task}。`;
  }
  function build(options = {}) {
    const en = options.language === "en";
    const activeDevice = options.deviceCode === true && options.existingLink !== true;
    const pipelineScope = activeDevice && options.allPipelines === true;
    const reuseDevice = options.reuseDevice === true && !activeDevice;
    if (options.deviceCode && options.existingLink && options.allPipelines)
      throw new Error("既有链接授权不能同时申请店铺流水线。");
    const shopId = pipelineScope ? identifier(options.shopId) : "";
    if (pipelineScope && !shopId) throw new Error("申请店铺流水线需要明确店铺 ID。");
    const productId = pipelineScope ? "" : identifier(options.product?.id);
    const permissions = [...new Set((options.permissions || []).filter((value) => permissionNames.has(value)))];
    if (activeDevice && !permissions.length) permissions.push("queue.view", "queue.process", "queue.retry");
    if (pipelineScope && permissions.some((permission) => !["queue.view", "queue.process", "queue.retry", "queue.monitor"].includes(permission)))
      throw new Error("店铺流水线只能申请进度看板、队列查看、处理和重试权限。");
    if (options.link && !options.deviceCode) {
      const link = new URL(options.link);
      if (link.username || link.password || origin(link.origin) !== origin(options.origin) || !["/staff", "/cli"].includes(link.pathname) || link.search || !/^#[A-Za-z0-9_-]{20,100}$/.test(link.hash))
        throw new Error("CLI 授权链接格式或站点不正确。");
    }
    const scope = pipelineScope
      ? (en ? `shop \`${shopId}\`'s currently approved product queues` : `店铺 \`${shopId}\` 当前获批准的商品队列`)
      : productId ? (en ? `product \`${productId}\`` : `商品 \`${productId}\``)
      : (en ? "the authorized products" : "已授权商品");
    const monitorOnly = permissions.includes("queue.monitor") && !permissions.includes("queue.process");
    const view = options.boardView || "active";
    if (monitorOnly && !["active", "processed"].includes(view)) throw new Error("看板视图仅支持 active 或 processed。");
    const task = monitorOnly
      ? (en ? `view only the \`${view}\` progress board for ${scope}` : `只查看${scope}的 \`${view}\` 进度看板`)
      : (en ? `carry out my requested work for ${scope}` : `管理${scope}`);
    const details = [];
    if (permissions.length) details.push(`${en ? "permissions: " : "权限："}\`${permissions.join(",")}\``);
    if (reuseDevice) details.push(en ? "reuse the already bound CLI device" : "复用已绑定的 CLI 设备");
    else if (options.deviceCode && options.existingLink) details.push(en ? "authorize through an existing product link with `--existing-link`" : "通过既有商品链接以 `--existing-link` 授权");
    else if (options.link && !options.deviceCode) details.push(en ? "log in with a separately supplied private link through `--link-stdin`" : "通过 `--link-stdin` 使用私下提供的链接登录");
    return sentence(options, task + (details.length ? (en ? ` (${details.join("; ")})` : `（${details.join("；")}）`) : ""));
  }
  function buildBoard(options = {}) {
    const productId = identifier(options.productId ?? options.product?.id);
    const shopId = identifier(options.shopId);
    if (!productId && !shopId) throw new Error("看板需要明确店铺或商品范围。");
    const view = options.view ?? "active";
    if (!["active", "processed"].includes(view)) throw new Error("看板视图仅支持 active 或 processed。");
    return build({ origin: options.origin, language: options.language,
      deviceCode: true, allPipelines: !productId, shopId,
      product: productId ? { id: productId } : undefined,
      permissions: ["queue.monitor"], boardView: view,
    });
  }
  function buildOwner(options = {}) {
    return sentence(options, options.language === "en"
      ? "carry out the shop management work I requested with owner authorization"
      : "在店主授权范围内完成我交代的店铺管理工作");
  }
  function buildProcessorWorkflow(options = {}) {
    const en = options.language === "en";
    const shopId = identifier(options.shopId), processorId = identifier(options.processorId), profileId = identifier(options.profileId);
    const shop = shopId ? (en ? `shop \`${shopId}\`'s ` : `店铺 \`${shopId}\` 的`) : "";
    const processor = processorId ? ` \`${processorId}\`` : "";
    const profile = profileId ? (en ? ` (configuration \`${profileId}\`)` : `（配置 \`${profileId}\`）`) : "";
    return sentence(options, en
      ? `configure ${shop}processor${processor}${profile} as I requested`
      : `按我的要求配置${shop}处理器${processor}${profile}`);
  }
  window.ExtoreCliPrompts = Object.freeze({ build, buildBoard, buildOwner, buildProcessorWorkflow });
})();
