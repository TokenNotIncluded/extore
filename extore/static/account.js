"use strict";

(() => {
  const mounts = new WeakMap();
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const rootScope = (auth) => auth?.role === "admin" && auth.superadmin === true && auth.shop_id === null;
  const shopScope = (auth) => auth?.role === "admin" && typeof auth.shop_id === "string" && !!auth.shop_id && auth.superadmin !== true;
  const sameAuthority = (a, b) => a?.role === "admin" && b?.role === "admin" && a.shop_id === b.shop_id && rootScope(a) === rootScope(b) && shopScope(a) === shopScope(b);
  const sameScope = (a, b) => sameAuthority(a, b) && a.session_id === b.session_id;
  const route = (path, hash = "") => {
    const legacy = /^#(invite|register|reset)=(.+)$/.exec(hash);
    const mode = path === "/account" && legacy ? legacy[1] : path.split("/")[2] || "login";
    return { mode: ["login", "invite", "register", "reset", "security"].includes(mode) ? mode : "login", token: legacy ? legacy[2] : hash.replace(/^#/, "") };
  };
  const field = (id, label, type = "text", attrs = "", value = "") => `<div class="field"><label for="${id}">${esc(label)}</label><input id="${id}" type="${type}" ${attrs} value="${esc(value)}"></div>`;
  const secret = (id, label, attrs = "") => field(id, label, "password", `autocomplete="off" ${attrs}`);
  const factors = (prefix) => `<div class="grid">${secret(prefix + "-code", "二次验证码（已启用时填写）", 'inputmode="numeric" maxlength="6"')}${secret(prefix + "-backup", "恢复码（与验证码二选一）", 'maxlength="100"')}</div>`;

  function mount({ root, api, auth = {}, mode = "login", token = "", isCurrent = () => true, navigate = (url) => { window.location.href = url; }, passkey, language = "zh-CN", onAuth = () => {} } = {}) {
    if (!root?.querySelector || typeof api !== "function") throw new TypeError("Account page needs a root and API");
    mounts.get(root)?.dispose();
    let disposed = false, version = 0, busy = false, selectedProfileShop = "";
    const controller = new AbortController();
    const active = () => !disposed && root.isConnected !== false && isCurrent();
    const $ = (selector) => root.querySelector(selector);
    const tr = (cn, en) => (typeof language === "function" ? language() : language) === "en" ? en : cn;
    const request = (path, body, method) => api(path, body, method, { signal: controller.signal, expectedScope: auth.shop_id || (rootScope(auth) ? "platform" : undefined), expectedSessionId: auth.session_id });
    const text = (selector, value) => { if (active() && $(selector)) $(selector).textContent = value; };
    const error = (e) => text("#account-error", e?.name === "NotAllowedError" ? tr("验证已取消，可以重试。", "Verification cancelled. You can retry.") : e?.message || tr("操作未完成，请重试。", "The operation did not complete. Retry."));
    const page = (title, body) => {
      if (!active()) return;
      version++;
      root.innerHTML = `<div class="account-page ${["security", "shops", "mail", "profiles"].includes(mode) ? "" : "narrow"}"><div class="section-head"><h${mode === "security" || ["shops", "mail", "profiles"].includes(mode) ? "2" : "1"}>${esc(title)}</h${mode === "security" || ["shops", "mail", "profiles"].includes(mode) ? "2" : "1"}><a href="/admin">${tr("商家后台", "Dashboard")}</a></div>${body}<p id="account-error" class="error" role="alert"></p><div id="account-confirmation"></div></div>`;
    };
    const run = async (handler, node) => {
      if (!active() || busy) return;
      busy = true;
      const revision = version;
      if (node) node.disabled = true;
      text("#account-error", "");
      try { await handler(); } catch (e) { if (active() && revision === version) error(e); }
      finally { busy = false; if (active() && node?.isConnected !== false) node.disabled = false; }
    };
    const on = (selector, event, handler) => { const node = $(selector); node?.addEventListener(event, (e) => { e.preventDefault(); return run(() => handler(e), node); }); };
    const values = (prefix) => {
      const value = { password: $("#" + prefix + "-password")?.value || "" };
      const code = $("#" + prefix + "-code")?.value.trim();
      const backup = $("#" + prefix + "-backup")?.value.trim();
      if (code && backup) throw new Error("验证码与恢复码请只填写一项");
      if (code) value.code = code;
      if (backup) value.backup_code = backup;
      return value;
    };
    const clearSecrets = () => root.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; });
    async function checkScope(allowFreshSession = false, verified = null) {
      const current = verified || await request("/auth/status");
      if (!active()) return false;
      if (!(allowFreshSession ? sameAuthority(auth, current) : sameScope(auth, current))) {
        clearSecrets();
        throw new Error("当前登录账户或店铺已改变，请重新打开后台后再操作");
      }
      if (allowFreshSession) { auth = current; onAuth(current); }
      return true;
    }
    function fresh(handler, title = "确认账户身份") {
      if (!active()) return;
      const target = $("#account-confirmation");
      target.innerHTML = `<section class="panel account-stepup"><h3>${esc(title)}</h3><p class="caption">重新验证后才会提交本次操作。店铺范围：${esc(rootScope(auth) ? "超级管理员" : auth.shop_id || "未登录")}</p>${shopScope(auth) ? `<form id="account-fresh-form">${secret("fresh-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${factors("fresh")}<button type="submit">使用密码确认</button></form>` : ""}<div class="actions"><button id="account-fresh-passkey" type="button">使用 Passkey 确认</button><button id="account-fresh-cancel" class="secondary" type="button">取消</button></div></section>`;
      on("#account-fresh-form", "submit", async () => {
        try { await request("/auth/reauth/password", values("fresh"), "POST"); } finally { clearSecrets(); }
        if (!await checkScope()) return;
        if (active()) target.innerHTML = "";
        await handler();
      });
      on("#account-fresh-passkey", "click", async () => {
        if (typeof passkey !== "function") throw new Error("Passkey 模块未加载，请刷新页面");
        const verified = await passkey(false, null, { signal: controller.signal, isCurrent: active });
        if (!await checkScope(true, verified)) return;
        if (active()) target.innerHTML = "";
        await handler();
      });
      on("#account-fresh-cancel", "click", () => { target.innerHTML = ""; });
    }
    const completed = (message) => { page(tr("操作已完成", "Done"), `<section class="panel"><p>${esc(message)}</p><a href="/account/login">${tr("返回登录", "Return to sign in")}</a></section>`); };

    function login() {
      page(tr("店铺账户登录", "Shop account sign in"), `<section class="panel"><form id="account-login-form">${field("login-email", tr("邮箱", "Email"), "email", 'required autocomplete="username" maxlength="254"')}${secret("login-password", tr("密码", "Password"), 'required autocomplete="current-password" maxlength="200"')}${factors("login")}<button class="full" type="submit">${tr("登录店铺", "Sign in")}</button></form><div class="form-divider"><button id="account-passkey" type="button" class="secondary full">${tr("使用 Passkey 登录", "Sign in with Passkey")}</button>${auth.password_enabled ? `<form id="account-bootstrap-form" class="form-divider">${secret("bootstrap-password", "超级管理员首次登录密码", 'required autocomplete="current-password"')}<button class="secondary full" type="submit">首次登录并添加 Passkey</button></form>` : ""}</div></section><p><a href="/account/reset">${tr("忘记密码", "Forgot password")}</a>${auth.registration_enabled === true ? ` · <a href="/account/register">${tr("注册店铺", "Register a shop")}</a>` : ""}</p>${auth.registration_enabled === true ? "" : `<p class="caption">${tr("注册当前关闭。请使用管理员发送的店铺邀请链接。", "Registration is closed. Use the shop invitation sent by an administrator.")}</p>`}`);
      on("#account-login-form", "submit", async () => {
        const body = { email: $("#login-email").value.trim(), ...values("login") };
        try { await request("/auth/email/login", body, "POST"); } finally { clearSecrets(); }
        if (active()) navigate("/admin");
      });
      on("#account-passkey", "click", async () => { await passkey(false, null, { signal: controller.signal, isCurrent: active }); if (active()) navigate("/admin"); });
      on("#account-bootstrap-form", "submit", async () => {
        try { await request("/auth/password", { password: $("#bootstrap-password").value }, "POST"); } finally { clearSecrets(); }
        if (active()) navigate("/admin");
      });
    }
    function emailFlow() {
      if (mode === "register" && auth.registration_enabled !== true) {
        page("店铺注册", '<section class="panel"><p>注册当前关闭，请使用管理员发送的邀请链接。</p><a href="/account/login">返回登录</a></section>'); return;
      }
      if (mode === "register" && token) {
        page("确认注册邮箱", '<section class="panel"><p>确认这封邮件是你申请注册时收到的，然后完成邮箱验证。</p><button id="account-confirm-register" type="button">验证邮箱并登录</button></section>');
        on("#account-confirm-register", "click", async () => { await request("/auth/register/email/confirm", { token }, "POST"); if (active()) navigate("/admin"); }); return;
      }
      if (mode === "invite" || (mode === "reset" && token)) {
        if (!/^[A-Za-z0-9_-]{20,100}$/.test(token)) { page("链接无效", '<p class="error">请打开邮件中完整的邀请或密码重置链接。</p>'); return; }
        page(mode === "invite" ? "领取店铺邀请" : "重置店铺密码", `<section class="panel"><form id="account-token-form">${secret("token-password", "新密码（至少 12 字符）", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${secret("token-confirm", "再次输入新密码", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${mode === "reset" ? factors("token") + '<p class="caption">已开启二次验证的账户仍需验证码或恢复码。</p>' : ""}<button class="full" type="submit">${mode === "invite" ? "设置密码并领取店铺" : "确认重置密码"}</button></form></section>`);
        on("#account-token-form", "submit", async () => {
          const body = { token, ...values("token") };
          if (body.password !== $("#token-confirm").value) throw new Error("两次密码不一致");
          try { await request(mode === "invite" ? "/auth/invite/claim" : "/auth/password/reset/confirm", body, "POST"); } finally { clearSecrets(); }
          if (!active()) return;
          if (mode === "invite") navigate("/admin"); else completed("密码已重置，请重新登录。此前设备会话已退出。");
        }); return;
      }
      page(mode === "register" ? "注册店铺" : "找回店铺密码", `<section class="panel"><form id="account-email-form">${field("email-email", "邮箱", "email", 'required autocomplete="username" maxlength="254"')}${mode === "register" ? field("email-name", "店铺名称", "text", 'required maxlength="100"') + secret("email-password", "密码（至少 12 字符）", 'required autocomplete="new-password" minlength="12" maxlength="200"') : ""}<button class="full" type="submit">${mode === "register" ? "发送邮箱验证邮件" : "发送密码重置邮件"}</button></form></section><p><a href="/account/login">返回登录</a></p>`);
      on("#account-email-form", "submit", async () => {
        const body = { email: $("#email-email").value.trim() };
        if (mode === "register") { body.name = $("#email-name").value.trim(); body.password = $("#email-password").value; }
        try { await request(mode === "register" ? "/auth/register/email/request" : "/auth/password/reset/request", body, "POST"); } finally { clearSecrets(); }
        if (active()) completed("若此邮箱符合条件，确认邮件会发送到你的邮箱。请打开邮件中的链接继续。");
      });
    }
    function recovery(codes) {
      if (!active()) return;
      if (!Array.isArray(codes) || !codes.length || codes.some((code) => typeof code !== "string" || code.length > 100)) throw new Error("恢复码返回异常，请重新生成");
      page("保存恢复码 · 仅显示这一次", `<section class="panel"><p>每个恢复码只能用一次。请保存到自己的密码管理器；离开此页后无法再次查看。</p><pre class="result account-recovery">${codes.map(esc).join("\n")}</pre><button id="account-recovery-close" type="button">我已保存，关闭恢复码</button></section>`);
      on("#account-recovery-close", "click", security);
    }
    async function security() {
      if (!rootScope(auth) && !shopScope(auth)) { if (active()) navigate("/account/login"); return; }
      const load = ++version;
      const [keys, account] = await Promise.all([request("/auth/passkeys"), shopScope(auth) ? request("/shop/account") : Promise.resolve(null)]);
      if (!active() || load !== version) return;
      if (account && account.id !== auth.shop_id) throw new Error("店铺账户范围不一致，请重新登录");
      page("账户安全", `<p class="caption">${account ? "当前店铺：" + esc(account.name) + " · " + esc(account.email) : "超级管理员 · 平台范围"}</p>${account ? `<section class="panel"><h3>店铺账户</h3><form id="account-name-form">${field("account-name", "店铺名称", "text", 'required maxlength="100"', account.name)}<button type="submit" class="secondary">保存名称</button></form><form id="account-password-form" class="form-divider"><h3>修改密码</h3>${secret("change-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${secret("change-new", "新密码（至少 12 字符）", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${factors("change")}<button type="submit">验证并修改密码</button><p class="caption">修改后退出此前所有设备及授权会话，当前浏览器重新登录。</p></form></section><section class="panel"><h3>密码二次验证</h3><p>${account.totp_enabled ? "已开启。密码登录需验证码或恢复码；Passkey 可独立登录。" : "尚未开启。可添加验证器，为密码登录增加二次验证。"}</p><form id="account-totp-form">${secret("totp-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${account.totp_enabled ? factors("totp") : ""}<div class="actions">${account.totp_enabled ? '<button id="account-totp-rotate" type="submit">重新生成恢复码</button><button id="account-totp-disable" type="button" class="danger">关闭二次验证</button>' : '<button type="submit">设置验证器</button>'}</div></form><div id="account-totp-setup"></div></section>` : '<p class="caption">超级管理员使用 Passkey。可添加多个设备；请保留备用设备。</p>'}<section class="panel"><h3>Passkey 设备</h3>${field("account-key-name", "新设备名称", "text", 'maxlength="200"', "备用 Passkey")}<button id="account-key-add" type="button">添加 Passkey</button><div class="product-list">${keys.map((key) => `<div class="product-row"><div class="product-info"><h3>${esc(key.name)}</h3><p>${esc(new Date(key.created * 1000).toLocaleString())}</p></div><button data-account-key="${esc(key.id)}" type="button" class="danger">移除</button></div>`).join("")}</div></section>`);
      on("#account-name-form", "submit", async () => { const name = $("#account-name").value.trim(); if (!await checkScope()) return; await request("/shop/account", { name }, "PATCH"); if (active()) await security(); });
      on("#account-password-form", "submit", async () => {
        const body = { ...values("change"), new_password: $("#change-new").value };
        if (!await checkScope()) return;
        try { await request("/auth/password/change", body, "POST"); } finally { clearSecrets(); }
        if (!await checkScope(true)) return;
        if (active()) await security();
      });
      on("#account-key-add", "click", () => { const name = $("#account-key-name").value; fresh(async () => { const verified = await passkey(true, name, { signal: controller.signal, isCurrent: active }); if (!await checkScope(true, verified)) return; if (active()) await security(); }, "确认添加 Passkey"); });
      root.querySelectorAll("[data-account-key]").forEach((node) => node.addEventListener("click", () => run(() => fresh(async () => { await request("/auth/passkeys/" + encodeURIComponent(node.dataset.accountKey), null, "DELETE"); if (active()) await security(); }, "确认移除 Passkey"), node)));
      on("#account-totp-form", "submit", async () => {
        const body = values("totp");
        if (!await checkScope()) return;
        if (account.totp_enabled) {
          try { const result = await request("/auth/totp/backup-codes", body, "POST"); if (active()) recovery(result.backup_codes); } finally { clearSecrets(); }
          return;
        }
        let result;
        try { result = await request("/auth/totp/setup", body, "POST"); } finally { clearSecrets(); }
        if (!active()) return;
        $("#account-totp-setup").innerHTML = `<div class="form-divider"><p>在验证器里手动添加下方密钥，或复制本地设置链接导入。密钥不发送给第三方二维码服务。</p>${field("totp-secret", "验证器密钥", "password", "readonly autocomplete=\"off\"", result.secret)}${field("totp-uri", "验证器设置链接", "password", "readonly autocomplete=\"off\"", result.uri)}<button id="account-totp-copy" type="button" class="secondary">复制设置链接</button><div class="field">${secret("totp-confirm", "验证器当前六位码", 'inputmode="numeric" maxlength="6"')}</div><button id="account-totp-confirm" type="button">确认开启二次验证</button></div>`;
        on("#account-totp-copy", "click", async () => { const copied = await window.ExtoreClipboard?.writeText($("#totp-uri").value); if (!copied) throw new Error("复制失败，请在输入框中手动复制"); });
        on("#account-totp-confirm", "click", async () => { try { const result = await request("/auth/totp/confirm", { code: $("#totp-confirm").value.trim() }, "POST"); if (active()) recovery(result.backup_codes); } finally { clearSecrets(); } });
      });
      on("#account-totp-disable", "click", async () => { const body = values("totp"); if (!await checkScope()) return; try { await request("/auth/totp/disable", body, "POST"); } finally { clearSecrets(); } if (active()) await security(); });
    }
    async function shops() {
      if (!rootScope(auth)) throw new Error("仅超级管理员可管理店铺");
      const load = ++version, items = await request("/platform/shops");
      if (!active() || version !== load) return;
      page("店铺", `<section class="panel"><h3>创建店铺并发送邀请</h3><form id="account-shop-form"><div class="grid">${field("shop-name", "店铺名称", "text", 'required maxlength="100"')}${field("shop-email", "店主邮箱", "email", 'required maxlength="254"')}</div><button type="submit">验证身份并创建店铺</button><p class="caption">邀请发送到店主邮箱，由店主验证邮箱并设置自己的密码。</p></form></section><div class="product-list">${items.map((shop) => `<article class="product-row"><div class="product-info"><h3>${esc(shop.name)}</h3><p>${esc(shop.email || "旧版店铺")} · ${shop.verified ? "已领取" : "待领取"} · ${shop.enabled ? "启用" : "停用"}</p><p class="mono caption">${esc(shop.id)}</p></div><div class="actions">${!shop.verified && shop.email ? `<button data-shop-invite="${esc(shop.id)}" class="secondary">重发邀请</button>` : ""}<button data-shop-toggle="${esc(shop.id)}" class="${shop.enabled ? "danger" : "secondary"}">${shop.enabled ? "停用店铺" : "启用店铺"}</button></div></article>`).join("")}</div>`);
      on("#account-shop-form", "submit", () => { const body = { name: $("#shop-name").value.trim(), email: $("#shop-email").value.trim() }; fresh(async () => { await request("/platform/shops", body, "POST"); if (active()) await shops(); }, "确认创建店铺"); });
      for (const [selector, key, action] of [["[data-shop-invite]", "shopInvite", "invite"], ["[data-shop-toggle]", "shopToggle", "toggle"]]) root.querySelectorAll(selector).forEach((node) => node.addEventListener("click", () => run(() => fresh(async () => { const item = items.find((shop) => shop.id === node.dataset[key]); await request("/platform/shops/" + encodeURIComponent(item.id) + (action === "invite" ? "/invite" : ""), action === "invite" ? {} : { enabled: !item.enabled }, action === "invite" ? "POST" : "PATCH"); if (active()) await shops(); }, action === "invite" ? "确认重发邀请" : "确认更改店铺状态"), node)));
    }
    async function mail() {
      if (!rootScope(auth)) throw new Error("仅超级管理员可配置邮箱服务器");
      const load = ++version, settings = await request("/platform/settings");
      if (!active() || version !== load) return;
      const smtp = settings.smtp || {};
      page("邮箱服务器与注册", `<section class="panel"><form id="account-mail-form"><label><input id="mail-enabled" type="checkbox" ${smtp.enabled ? "checked" : ""}>启用邮箱服务</label><div class="grid">${field("mail-host", "SMTP 主机", "text", 'maxlength="253"', smtp.host)}${field("mail-port", "端口", "number", 'min="1" max="65535"', smtp.port || 587)}<div class="field"><label for="mail-mode">加密方式</label><select id="mail-mode"><option value="starttls" ${smtp.mode !== "ssl" ? "selected" : ""}>STARTTLS</option><option value="ssl" ${smtp.mode === "ssl" ? "selected" : ""}>TLS / SSL</option></select></div>${field("mail-sender", "发件邮箱", "email", 'maxlength="254"', smtp.sender)}${field("mail-name", "发件人名称", "text", 'maxlength="100"', smtp.from_name)}${secret("mail-user", "SMTP 用户名（留空保留）", 'autocomplete="off" maxlength="320"')}${secret("mail-password", "SMTP 密码（留空保留已保存的密码）", 'autocomplete="new-password" maxlength="1000"')}</div><p class="caption">${smtp.password_configured || smtp.has_password ? "已保存 SMTP 密码。页面不会回读密码；留空保持原值。" : "尚未保存 SMTP 密码。"}</p><div class="form-divider"><label><input id="mail-registration" type="checkbox" ${settings.registration_enabled ? "checked" : ""}>开放店铺注册（须验证邮箱）</label><p class="caption">默认关闭。启用注册前须先配置可用的邮箱服务器。</p></div><button type="submit">验证身份并保存</button></form></section>`);
      on("#account-mail-form", "submit", () => {
        const body = { registration_enabled: $("#mail-registration").checked, smtp: { enabled: $("#mail-enabled").checked, host: $("#mail-host").value.trim(), port: Number($("#mail-port").value), mode: $("#mail-mode").value, sender: $("#mail-sender").value.trim(), from_name: $("#mail-name").value.trim() } };
        if ($("#mail-user").value) body.smtp.username = $("#mail-user").value;
        if ($("#mail-password").value) body.smtp.password = $("#mail-password").value;
        clearSecrets();
        fresh(async () => { try { await request("/platform/settings", body, "PUT"); } finally { delete body.smtp.password; delete body.smtp.username; } if (active()) await mail(); }, "确认更改邮箱与注册设置");
      });
    }
    async function profiles() {
      if (!rootScope(auth) && !shopScope(auth)) throw new Error("仅店主可管理处理器账户");
      const load = ++version;
      const [catalog, shopsList] = await Promise.all([request("/admin/processors"), rootScope(auth) ? request("/platform/shops") : Promise.resolve([])]);
      if (!active() || version !== load) return;
      if (rootScope(auth) && !selectedProfileShop) selectedProfileShop = shopsList[0]?.id || "";
      const items = rootScope(auth) && !selectedProfileShop ? [] : await request("/admin/processor-profiles" + (rootScope(auth) ? "?" + new URLSearchParams({ shop_id: selectedProfileShop }) : ""));
      if (!active() || version !== load) return;
      const specs = Array.isArray(catalog) ? catalog : catalog.processors || [];
      page("商品处理器账户", `<p class="caption">配置独立保存在店铺中。仅店主可更新与绑定，页面只显示名称、范围和版本，不回读付款账号或密钥。</p><section class="panel"><h3>新建处理器账户</h3><form id="profile-create-form">${rootScope(auth) ? `<div class="field"><label for="profile-shop">所属店铺</label><select id="profile-shop"><option value="">请选择店铺</option>${shopsList.map((shop) => `<option value="${esc(shop.id)}" ${selectedProfileShop === shop.id ? "selected" : ""}>${esc(shop.name)}</option>`).join("")}</select></div>` : ""}${field("profile-name", "账户名称", "text", 'required maxlength="100"')}<div class="field"><label for="profile-processor">商品处理器</label><select id="profile-processor"><option value="">请选择处理器</option>${specs.map((spec) => `<option value="${esc(spec.id)}">${esc(typeof spec.name === "object" ? spec.name["zh-CN"] || spec.id : spec.name || spec.id)}</option>`).join("")}</select></div><div id="profile-fields"></div><button type="submit">创建处理器账户</button></form></section><div id="profile-items">${items.map((item) => `<section class="panel"><div class="section-head"><div><h3>${esc(item.name)}</h3><p class="caption mono">${esc(item.processor_id)} · ${esc(item.shop_id)} · 版本 ${esc(item.revision)}${item.disabled ? " · 已停用" : ""}</p></div><div class="actions">${item.disabled ? "" : `<button data-profile-edit="${esc(item.id)}" class="secondary">更新账户</button><button data-profile-delete="${esc(item.id)}" class="danger">停用</button>`}</div></div><div id="profile-editor-${esc(item.id)}"></div></section>`).join("")}</div>`);
      const definitions = (spec) => spec?.shop_configuration || spec?.configuration || [];
      const fieldsHTML = (spec, prefix, existing) => definitions(spec).map((definition, index) => secret(prefix + index, (typeof definition.label === "object" ? definition.label["zh-CN"] || definition.key : definition.label || definition.key) + (existing ? "（留空保留）" : definition.required ? " *" : ""), `maxlength="10000" ${definition.required && !existing ? "required" : ""}`)).join("") || '<p class="caption">此处理器没有店铺账户配置项。</p>';
      const capture = (spec, prefix) => Object.fromEntries(definitions(spec).map((definition, index) => [definition.key, $("#" + prefix + index)?.value || ""]).filter(([, value]) => value !== ""));
      on("#profile-shop", "change", async () => { selectedProfileShop = $("#profile-shop").value; await profiles(); });
      on("#profile-processor", "change", () => { $("#profile-fields").innerHTML = fieldsHTML(specs.find((spec) => spec.id === $("#profile-processor").value), "profile-field-", false); });
      on("#profile-create-form", "submit", async () => {
        const spec = specs.find((spec) => spec.id === $("#profile-processor").value);
        if (!spec) throw new Error("请选择可用的商品处理器");
        const body = { name: $("#profile-name").value.trim(), processor_id: spec.id, configuration: capture(spec, "profile-field-") };
        if (rootScope(auth)) { body.shop_id = $("#profile-shop").value; if (!body.shop_id) throw new Error("请选择所属店铺"); }
        clearSecrets();
        fresh(async () => { try { await request("/admin/processor-profiles", body, "POST"); } finally { body.configuration = {}; } if (active()) await profiles(); }, "确认创建处理器账户");
      });
      root.querySelectorAll("[data-profile-edit]").forEach((node) => node.addEventListener("click", () => run(() => {
        const item = items.find((p) => p.id === node.dataset.profileEdit), spec = specs.find((s) => s.id === item.processor_id), prefix = "profile-update-" + item.id + "-";
        $("#profile-editor-" + item.id).innerHTML = `<form id="profile-update-${esc(item.id)}">${field(prefix + "name", "账户名称", "text", 'required maxlength="100"', item.name)}${fieldsHTML(spec, prefix, true)}<p class="caption">留空保留原有配置，只替换填写的字段。</p><button type="submit">保存更新</button></form>`;
        on("#profile-update-" + item.id, "submit", async () => { const body = { name: $("#" + prefix + "name").value.trim() }, configuration = capture(spec, prefix); if (Object.keys(configuration).length) body.configuration = configuration; clearSecrets(); fresh(async () => { try { await request("/admin/processor-profiles/" + encodeURIComponent(item.id), body, "PUT"); } finally { delete body.configuration; } if (active()) await profiles(); }, "确认更新处理器账户"); });
      }, node)));
      root.querySelectorAll("[data-profile-delete]").forEach((node) => node.addEventListener("click", () => run(() => fresh(async () => { await request("/admin/processor-profiles/" + encodeURIComponent(node.dataset.profileDelete), null, "DELETE"); if (active()) await profiles(); }, "确认停用处理器账户"), node)));
    }
    const instance = Object.freeze({ dispose() { disposed = true; version++; controller.abort(); clearSecrets(); if (mounts.get(root) === instance) mounts.delete(root); }, confirmFresh: fresh, get active() { return active(); } });
    mounts.set(root, instance);
    const ready = mode === "confirm" ? () => page("确认身份", "") : mode === "security" ? security : mode === "shops" ? shops : mode === "mail" ? mail : mode === "profiles" ? profiles : mode === "login" ? login : emailFlow;
    instanceReady(ready);
    async function instanceReady(handler) { try { await handler(); } catch (e) { if (active()) { if (!root.querySelector("#account-error")) page("账户", ""); error(e); } } }
    return instance;
  }
  window.ExtoreAccount = Object.freeze({ mount, route, rootScope, shopScope, sameScope });
})();
