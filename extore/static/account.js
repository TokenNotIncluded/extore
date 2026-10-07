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
  const secret = (id, label, attrs = "") => field(id, label, "password", attrs.includes("autocomplete=") ? attrs : `autocomplete="off" ${attrs}`);
  const factors = (prefix, tr = (cn) => cn) => `<div class="grid">${secret(prefix + "-code", tr("2FA 验证码（已启用时填写）", "2FA code (if enabled)"), 'inputmode="numeric" autocomplete="one-time-code" maxlength="6"')}${secret(prefix + "-backup", tr("恢复码（与验证码二选一）", "Recovery code (instead of a 2FA code)"), 'maxlength="100"')}</div>`;

  function mount({ root, api, auth = {}, mode = "login", token = "", isCurrent = () => true, navigate = (url) => { window.location.href = url; }, passkey, language = "zh-CN", onAuth = () => {}, copyProcessorPrompt } = {}) {
    if (!root?.querySelector || typeof api !== "function") throw new TypeError("Account page needs a root and API");
    mounts.get(root)?.dispose();
    let disposed = false, version = 0, busy = false, selectedProfileShop = "", enrollment = null, contributionView = null;
    const controller = new AbortController();
    const active = () => !disposed && root.isConnected !== false && isCurrent();
    const $ = (selector) => root.querySelector(selector);
    const tr = (cn, en) => (typeof language === "function" ? language() : language) === "en" ? en : cn;
    const request = (path, body, method) => api(path, body, method, { signal: controller.signal, expectedScope: auth.shop_id || (rootScope(auth) ? "platform" : undefined), expectedSessionId: auth.session_id });
    const text = (selector, value) => { if (active() && $(selector)) $(selector).textContent = value; };
    const error = (e) => text("#account-error", e?.name === "NotAllowedError" ? tr("验证已取消，可以重试。", "Verification cancelled. You can retry.") : e?.message || tr("操作未完成，请重试。", "The operation did not complete. Retry."));
    const page = (title, body) => {
      if (!active()) return;
      clearContribution();
      clearTotpSetup();
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
    const clearSecrets = () => root.querySelectorAll('input[type="password"]').forEach((input) => { if (input !== enrollment?.uriInput) input.value = ""; });
    function clearTotpSetup() {
      const previous = enrollment;
      enrollment = null;
      if (!previous) return;
      clearTimeout(previous.timer);
      for (const input of [previous.secretInput, previous.uriInput, previous.codeInput]) {
        input.value = "";
        input.defaultValue = "";
      }
      previous.container.innerHTML = "";
      previous.form.hidden = false;
    }
    function clearContribution() { contributionView?.dispose(); contributionView = null; }
    const leavePage = () => { version++; clearContribution(); clearTotpSetup(); clearSecrets(); };
    window.addEventListener?.("pagehide", leavePage);
    async function checkScope(allowFreshSession = false, verified = null) {
      const current = verified || await request("/auth/status");
      if (!active()) return false;
      if (!(allowFreshSession ? sameAuthority(auth, current) : sameScope(auth, current))) {
        clearSecrets();
        clearTotpSetup();
        throw new Error("当前登录账户或店铺已改变，请重新打开后台后再操作");
      }
      if (allowFreshSession) { auth = current; onAuth(current); }
      return true;
    }
    function showTotpSetup(result) {
      if (!active()) return;
      const uri = new URL(result.uri);
      if (typeof result.secret !== "string" || !/^[A-Z2-7]{16,128}$/.test(result.secret) || uri.protocol !== "otpauth:" || uri.hostname !== "totp" || uri.searchParams.get("secret") !== result.secret || !Number.isFinite(result.expires) || result.expires * 1000 <= Date.now()) {
        throw new Error(tr("验证器设置无效或已过期，请重新设置。", "Authenticator setup is invalid or expired. Start again."));
      }
      clearTotpSetup();
      const container = $("#account-totp-setup"), form = $("#account-totp-form"), revision = version;
      container.innerHTML = `<section class="totp-setup form-divider"><h4>${tr("添加到验证器", "Add to your authenticator")}</h4><div class="totp-methods" role="group" aria-label="${tr("验证器设置方式", "Authenticator setup method")}"><button id="account-totp-qr" type="button" class="secondary" aria-pressed="true">${tr("二维码", "QR code")}</button><button id="account-totp-manual" type="button" class="secondary" aria-pressed="false">${tr("手动输入", "Manual entry")}</button></div><div id="totp-qr-panel"><p>${tr("用验证器扫描二维码；如果验证器就在这台手机上，可切换到手动输入。", "Scan with your authenticator. If it is on this phone, use manual entry.")}</p><div id="totp-qr-code" class="totp-qr-code" role="img" aria-label="${tr("验证器设置二维码", "Authenticator setup QR code")}"></div></div><div id="totp-manual-panel" hidden><p>${tr("在验证器中选择添加账号、手动输入设置密钥，再填入下方信息。", "Choose add account and enter a setup key manually in your authenticator.")}</p><div class="field"><label for="totp-secret">${tr("密钥（Secret）", "Setup key (Secret)")}</label><div class="totp-key-entry"><input id="totp-secret" type="text" readonly autocomplete="off" autocapitalize="off" spellcheck="false" aria-describedby="totp-key-help"><button id="account-totp-copy-secret" type="button" class="secondary">${tr("复制密钥", "Copy key")}</button></div><p id="totp-key-help" class="caption">${tr("类型选择「基于时间」，6 位验证码，每 30 秒更新。", "Select time based, 6 digits, updating every 30 seconds.")}</p></div><details class="totp-import"><summary>${tr("其他导入方式", "Other import options")}</summary>${field("totp-uri", tr("验证器设置链接", "Authenticator setup URI"), "password", 'readonly autocomplete="off"')}<button id="account-totp-copy" type="button" class="secondary">${tr("复制设置链接", "Copy setup URI")}</button></details></div><p id="totp-copy-status" class="caption" role="status"></p><p class="caption">${tr("设置有效期 10 分钟。二维码在本机生成；输入验证码确认后才会开启 2FA。", "Setup expires in 10 minutes. The QR code is generated locally. 2FA starts only after you confirm a code.")}</p><form id="account-totp-confirm-form" class="form-divider">${field("totp-confirm", tr("验证器当前六位码", "Current 6-digit authenticator code"), "text", 'required inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" minlength="6" maxlength="6"')}<div class="actions"><button id="account-totp-confirm" type="submit">${tr("确认开启 2FA", "Enable 2FA")}</button><button id="account-totp-cancel" type="button" class="secondary">${tr("取消设置", "Cancel setup")}</button></div></form></section>`;
      const state = { container, form, revision, expires: result.expires * 1000, secretInput: $("#totp-secret"), uriInput: $("#totp-uri"), codeInput: $("#totp-confirm"), confirming: false, timer: null };
      enrollment = state;
      state.secretInput.value = result.secret;
      state.uriInput.value = result.uri;
      form.hidden = true;
      text("#account-totp-notice", "");
      const current = () => active() && version === revision && enrollment === state;
      const expire = () => { if (!current()) return; clearTotpSetup(); text("#account-totp-notice", tr("设置已过期，2FA 尚未开启。请重新设置验证器。", "Setup expired. 2FA has not been enabled. Start again.")); };
      const usable = () => { if (!current()) return false; if (Date.now() >= state.expires) { expire(); return false; } return true; };
      state.timer = setTimeout(expire, Math.min(600000, Math.max(0, state.expires - Date.now())));
      const method = (manual) => {
        if (!usable()) return;
        $("#totp-qr-panel").hidden = manual;
        $("#totp-manual-panel").hidden = !manual;
        $("#account-totp-qr").setAttribute("aria-pressed", String(!manual));
        $("#account-totp-manual").setAttribute("aria-pressed", String(manual));
      };
      try {
        $("#totp-qr-code").innerHTML = window.ExtoreTotpQr.createSvg(state.uriInput.value);
        $("#totp-qr-code").querySelector("svg")?.setAttribute("aria-hidden", "true");
      } catch {
        method(true);
        $("#account-totp-qr").disabled = true;
        text("#totp-copy-status", tr("二维码暂不可用，请使用手动输入。", "QR code unavailable. Use manual entry."));
      }
      on("#account-totp-qr", "click", () => method(false));
      on("#account-totp-manual", "click", () => method(true));
      const copy = async (input, message) => {
        if (!usable() || !await checkScope() || !usable()) return;
        let copied = false;
        try { copied = await window.ExtoreClipboard?.writeText(input.value); } catch { /* Manual selection remains available. */ }
        if (!usable()) return;
        if (copied) text("#totp-copy-status", message);
        else {
          input.focus(); input.select(); input.setSelectionRange(0, input.value.length);
          text("#totp-copy-status", tr("复制失败，请长按已选中的内容手动复制。", "Copy failed. Press and hold the selected text to copy manually."));
        }
      };
      on("#account-totp-copy-secret", "click", () => copy(state.secretInput, tr("密钥已复制。", "Setup key copied.")));
      on("#account-totp-copy", "click", () => copy(state.uriInput, tr("设置链接已复制。", "Setup URI copied.")));
      $("#account-totp-cancel").addEventListener("click", () => {
        if (!current() || state.confirming) return;
        clearTotpSetup();
        text("#account-totp-notice", tr("已关闭设置，2FA 尚未开启。重新设置会生成新密钥。", "Setup closed. 2FA has not been enabled. Starting again creates a new key."));
      });
      on("#account-totp-confirm-form", "submit", async () => {
        if (!usable()) return;
        const code = state.codeInput.value.trim();
        if (!/^[0-9]{6}$/.test(code)) throw new Error(tr("请输入验证器当前的六位验证码。", "Enter the current 6-digit authenticator code."));
        state.confirming = true;
        $("#account-totp-cancel").disabled = true;
        try {
          if (!await checkScope() || !usable()) return;
          const response = await request("/auth/totp/confirm", { code }, "POST");
          if (current()) recovery(response.backup_codes);
        } finally {
          state.codeInput.value = "";
          state.confirming = false;
          if (current()) $("#account-totp-cancel").disabled = false;
        }
      });
    }
    function fresh(handler, title = "确认账户身份") {
      if (!active()) return;
      const target = $("#account-confirmation");
      const identity = shopScope(auth)
        ? `<div class="account-identity"><p id="account-fresh-shop"><strong>${esc(auth.shop_name || tr("店铺", "Shop"))}</strong></p>${auth.shop_email ? `<p id="account-fresh-email" class="caption">${esc(auth.shop_email)}</p>` : ""}<p class="caption mono">${tr("店铺 ID", "Shop ID")}：${esc(auth.shop_id)}</p></div>`
        : `<p class="caption">${rootScope(auth) ? tr("超级管理员", "Platform administrator") : tr("未登录", "Not signed in")}</p>`;
      target.innerHTML = `<section class="panel account-stepup"><h3>${esc(title)}</h3><p class="caption">${tr("重新验证后才会提交本次操作。", "This operation is submitted only after you verify your identity again.")}</p>${identity}${shopScope(auth) ? `<form id="account-fresh-form">${secret("fresh-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${factors("fresh", tr)}<button type="submit">使用密码确认</button></form>` : ""}<div class="actions"><button id="account-fresh-passkey" type="button">使用 Passkey 确认</button><button id="account-fresh-cancel" class="secondary" type="button">取消</button></div></section>`;
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
      page(tr("店铺账户登录", "Shop account sign in"), `<section class="panel"><form id="account-login-form">${field("login-email", tr("邮箱", "Email"), "email", 'required autocomplete="username" maxlength="254"')}${secret("login-password", tr("密码", "Password"), 'required autocomplete="current-password" maxlength="200"')}${factors("login", tr)}<button class="full" type="submit">${tr("登录店铺", "Sign in")}</button></form><div class="form-divider"><button id="account-passkey" type="button" class="secondary full">${tr("使用 Passkey 登录", "Sign in with Passkey")}</button>${auth.password_enabled ? `<form id="account-bootstrap-form" class="form-divider">${secret("bootstrap-password", "超级管理员首次登录密码", 'required autocomplete="current-password"')}<button class="secondary full" type="submit">首次登录并添加 Passkey</button></form>` : ""}</div></section><p><a href="/account/reset">${tr("忘记密码", "Forgot password")}</a>${auth.registration_enabled === true ? ` · <a href="/account/register">${tr("注册店铺", "Register a shop")}</a>` : ""}</p>${auth.registration_enabled === true ? "" : `<p class="caption">${tr("注册当前关闭。请使用管理员发送的店铺邀请链接。", "Registration is closed. Use the shop invitation sent by an administrator.")}</p>`}`);
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
        page(mode === "invite" ? "领取店铺邀请" : "重置店铺密码", `<section class="panel"><form id="account-token-form">${secret("token-password", "新密码（至少 12 字符）", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${secret("token-confirm", "再次输入新密码", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${mode === "reset" ? factors("token", tr) + '<p class="caption">已开启 2FA 的账户仍需验证码或恢复码。</p>' : ""}<button class="full" type="submit">${mode === "invite" ? "设置密码并领取店铺" : "确认重置密码"}</button></form></section>`);
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
      if (account) {
        auth = { ...auth, shop_name: account.name, shop_email: account.email || "" };
        onAuth(auth);
      }
      page(tr("账户安全", "Account security"), `<p class="caption">${account ? "当前店铺：" + esc(account.name) + " · " + esc(account.email) : "超级管理员 · 平台范围"}</p>${account ? `<section class="panel"><h3>店铺账户</h3><form id="account-name-form">${field("account-name", "店铺名称", "text", 'required maxlength="100"', account.name)}<button type="submit" class="secondary">保存名称</button></form><details class="account-password-change form-divider"><summary>修改密码</summary><form id="account-password-form">${secret("change-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${secret("change-new", "新密码（至少 12 字符）", 'required autocomplete="new-password" minlength="12" maxlength="200"')}${factors("change", tr)}<button type="submit">验证并修改密码</button><p class="caption">修改后退出此前所有设备及授权会话，当前浏览器重新登录。</p></form></details></section><section class="panel"><h3>${tr("2FA 双因素认证", "2FA two-factor authentication")}</h3><p>${account.totp_enabled ? tr("已开启。密码登录需要验证器验证码或恢复码；Passkey 可独立登录。", "Enabled. Password sign-in requires an authenticator or recovery code. Passkeys work independently.") : tr("尚未开启。添加验证器，让密码登录多一层保护。", "Not enabled. Add an authenticator to protect password sign-in.")}</p><form id="account-totp-form">${secret("totp-password", "当前密码", 'required autocomplete="current-password" maxlength="200"')}${account.totp_enabled ? factors("totp", tr) : ""}<div class="actions">${account.totp_enabled ? `<button id="account-totp-rotate" type="submit">${tr("重新生成恢复码", "Regenerate recovery codes")}</button><button id="account-totp-disable" type="button" class="danger">${tr("关闭 2FA", "Disable 2FA")}</button>` : `<button type="submit">${tr("设置验证器", "Set up authenticator")}</button>`}</div></form><div id="account-totp-setup"></div><p id="account-totp-notice" class="caption" role="status"></p></section>` : '<p class="caption">超级管理员使用 Passkey。可添加多个设备；请保留备用设备。</p>'}<section class="panel"><h3>Passkey 设备</h3>${field("account-key-name", "新设备名称", "text", 'maxlength="200"', "备用 Passkey")}<button id="account-key-add" type="button">添加 Passkey</button><div class="product-list">${keys.map((key) => `<div class="product-row"><div class="product-info"><h3>${esc(key.name)}</h3><p>${esc(new Date(key.created * 1000).toLocaleString())}</p></div><button data-account-key="${esc(key.id)}" type="button" class="danger">移除</button></div>`).join("")}</div></section>`);
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
        const revision = version;
        const body = values("totp");
        if (!await checkScope()) return;
        if (account.totp_enabled) {
          try { const result = await request("/auth/totp/backup-codes", body, "POST"); if (active()) recovery(result.backup_codes); } finally { clearSecrets(); }
          return;
        }
        let result;
        try { result = await request("/auth/totp/setup", body, "POST"); } finally { clearSecrets(); }
        try {
          if (!active() || revision !== version || !await checkScope() || revision !== version) return;
          showTotpSetup(result);
        } finally { result.secret = result.uri = ""; }
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
      if (!rootScope(auth) && !shopScope(auth)) throw new Error("仅店主可管理处理器配置");
      const load = ++version;
      const [catalog, shopsList] = await Promise.all([request("/admin/processors"), rootScope(auth) ? request("/platform/shops") : Promise.resolve([])]);
      if (!active() || version !== load) return;
      if (rootScope(auth) && !selectedProfileShop) selectedProfileShop = shopsList[0]?.id || "";
      const profileShop = rootScope(auth) ? selectedProfileShop : auth.shop_id;
      const items = rootScope(auth) && !profileShop ? [] : await request("/admin/processor-profiles" + (rootScope(auth) ? "?" + new URLSearchParams({ shop_id: profileShop }) : ""));
      if (!active() || version !== load) return;
      if (!Array.isArray(items) || items.some((item) => !item || item.shop_id !== profileShop)) throw new Error("处理器配置的店铺范围不一致，请重新打开后台");
      const specs = Array.isArray(catalog) ? catalog : catalog.processors || [];
      const definitions = (spec) => {
        const fields = new Map(), declared = spec?.shop_configuration || spec?.configuration || [];
        for (const definition of Array.isArray(declared) ? declared : []) {
          if (!definition || typeof definition.key !== "string") continue;
          if (fields.has(definition.key)) {
            const sensitive = { ...fields.get(definition.key), secret: true };
            delete sensitive.default;
            fields.set(definition.key, sensitive);
          } else fields.set(definition.key, definition);
        }
        return [...fields.values()];
      };
      const label = (value, fallback) => typeof value === "object" && value ? tr(value["zh-CN"] || fallback, value.en || fallback) : value || fallback;
      const choices = (definition) => {
        const options = definition.options;
        if (!Array.isArray(options) || !options.length || options.some((option) => !option || typeof option.value !== "string" || !option.value)) throw new Error(tr("处理器选项配置无效，请更新处理器。", "Processor choices are invalid. Update the processor."));
        if (new Set(options.map((option) => option.value)).size !== options.length) throw new Error(tr("处理器选项不能重复，请更新处理器。", "Processor choices must be unique. Update the processor."));
        return options;
      };
      const limits = [
        { key: "timeout_seconds", name: "超时（秒）", min: 10, max: 120, default: 120 },
        { key: "memory_mb", name: "内存（MiB）", min: 64, max: 512, default: 256 },
        { key: "cpu_seconds", name: "CPU 时间（秒）", min: 1, max: 120, default: 120 },
        { key: "max_output_bytes", name: "输出上限（字节）", min: 65536, max: 1000000, default: 1000000 },
      ];
      const validName = (name) => /^[A-Z][A-Z0-9_]{0,63}$/.test(name) && !name.startsWith("EXTORE_");
      const drafts = new Map();
      const fieldsHTML = (spec, prefix, existing, configuration = {}) => definitions(spec).map((definition, index) => {
        const sensitive = definition.secret !== false, id = prefix + index;
        const name = label(definition.label, definition.key) + (existing && sensitive ? tr("（留空保留）", " (leave empty to keep)") : definition.required ? " *" : "");
        const max = Number.isInteger(definition.max_length) && definition.max_length > 0 ? Math.min(definition.max_length, 10000) : 10000;
        const description = label(definition.description, "");
        const help = description ? `<p id="${id}-help" class="caption">${esc(description)}</p>` : "";
        const attrs = `maxlength="${max}" ${definition.required && (!existing || !sensitive) ? "required" : ""}${help ? ` aria-describedby="${id}-help"` : ""}`;
        // A secret is never filled from stored configuration or a default, even
        // when the code declares an enumerated choice for it.
        if (sensitive) return secret(id, name, attrs) + help;
        const value = existing ? configuration[definition.key] ?? "" : definition.default ?? "";
        if (definition.type === "select") {
          const options = choices(definition), invalid = value !== "" && !options.some((option) => option.value === value);
          return `<div class="field"><label for="${id}">${esc(name)}</label><select id="${id}" ${definition.required ? "required" : ""}${help ? ` aria-describedby="${id}-help"` : ""}><option value="" ${value === "" ? "selected" : ""}>${esc(existing && !definition.required ? tr("清空此选项", "Clear this option") : tr("请选择", "Choose an option"))}</option>${invalid ? `<option value="${esc(value)}" disabled selected>${esc(tr("无效选项，请重新选择", "Invalid option, choose again"))}</option>` : ""}${options.map((option) => `<option value="${esc(option.value)}" ${value === option.value ? "selected" : ""}>${esc(label(option.label, option.value))}</option>`).join("")}</select>${help}</div>`;
        }
        if (definition.type === "textarea") return `<div class="field"><label for="${id}">${esc(name)}</label><textarea id="${id}" rows="5" ${attrs}>${esc(value)}</textarea></div>${help}`;
        return field(id, name, ["text", "url", "email", "tel", "number"].includes(definition.type) ? definition.type : "text", attrs, value) + help;
      }).join("") || '<p class="caption">此处理器没有额外参数。</p>';
      const rowHTML = (prefix, kind, row) => {
        const base = `${prefix}${kind}-${row.id}`;
        return `<div id="${base}" class="processor-${kind === "variable" ? "variable" : "secret"}-row">${field(base + "-name", "名称（大写）", "text", `maxlength="64" pattern="[A-Z][A-Z0-9_]{0,63}" ${kind === "secret" && row.originalName ? "readonly" : ""}`, row.name)}${kind === "secret" ? secret(base + "-value", row.originalName ? "新值（留空保留）" : "密钥值", 'maxlength="8192"') : `<div class="field"><label for="${base}-value">值（普通文本）</label><textarea id="${base}-value" rows="2" maxlength="8192">${esc(row.value)}</textarea></div>`}<button id="${base}-remove" type="button" class="secondary">删除${kind === "secret" ? "密钥" : "变量"}</button></div>`;
      };
      const editorHTML = (spec, prefix, item = null) => {
        const workflow = item?.workflow || {}, state = { variables: [], secrets: [], nextId: 0 };
        for (const [name, value] of Object.entries(workflow.variables || {})) if (typeof value === "string") state.variables.push({ id: state.nextId++, name, value, originalName: name });
        for (const name of Array.isArray(workflow.configured_secret_names) ? workflow.configured_secret_names : []) if (typeof name === "string") state.secrets.push({ id: state.nextId++, name, originalName: name });
        drafts.set(prefix, state);
        return `${spec?.description ? `<p class="caption processor-description">${esc(label(spec.description, ""))}</p>` : ""}<fieldset class="processor-settings"><legend>处理器参数</legend>${fieldsHTML(spec, prefix, !!item, item?.configuration || {})}<p class="caption">普通模板可直接编辑；处理器凭据不回读，留空保留。</p></fieldset><details class="processor-workflow" ${state.variables.length ? "open" : ""}><summary id="${prefix}variable-summary">普通变量 · ${state.variables.length}</summary><fieldset class="processor-workflow-fields"><legend>普通变量</legend><p class="caption">名称使用大写字母、数字与下划线；系统保留名不能使用，保存时会校验。程序通过 Task.environment[NAME] 读取，同时传入 EXTORE_WORKFLOW_NAME。普通变量可见，请把凭据放入密钥。</p><div id="${prefix}variables">${state.variables.map((row) => rowHTML(prefix, "variable", row)).join("")}</div><div class="processor-workflow-actions actions"><button id="${prefix}add-variable" type="button" class="secondary">添加变量</button></div></fieldset></details><details class="processor-workflow" ${state.secrets.length ? "open" : ""}><summary id="${prefix}secret-summary">密钥 · ${state.secrets.length}</summary><fieldset class="processor-workflow-fields"><legend>密钥</legend><p class="caption">只显示已配置的名称，值只写入、不回读。留空保留；删除在保存后生效。改名请删除旧密钥，再添加新名称和值。</p><div id="${prefix}secrets">${state.secrets.map((row) => rowHTML(prefix, "secret", row)).join("")}</div><div class="processor-workflow-actions actions"><button id="${prefix}add-secret" type="button" class="secondary">添加密钥</button></div></fieldset></details><details class="processor-workflow"><summary>运行限制</summary><fieldset class="processor-workflow-fields"><legend>运行限制</legend><div class="processor-runtime-grid grid">${limits.map((limit) => field(prefix + "runtime-" + limit.key, limit.name, "number", `required min="${limit.min}" max="${limit.max}" step="1" inputmode="numeric"`, workflow.runtime?.[limit.key] ?? limit.default)).join("")}</div><p class="caption">超时 10–120 秒，内存 64–512 MiB，CPU 时间 1–120 秒，输出 65,536–1,000,000 字节。仅运行离线预设处理器；配置不能改变启动命令、镜像、软件包、网络或挂载。</p></fieldset></details>`;
      };
      const wireWorkflow = (prefix) => {
        const state = drafts.get(prefix);
        const updateCount = (kind, rows) => text("#" + prefix + kind + "-summary", (kind === "variable" ? "普通变量" : "密钥") + " · " + rows.filter((row) => !row.deleted).length);
        const bindRow = (kind, row) => on(`#${prefix}${kind}-${row.id}-remove`, "click", () => {
          row.deleted = true;
          const node = $(`#${prefix}${kind}-${row.id}`), value = $(`#${prefix}${kind}-${row.id}-value`);
          if (value) value.value = value.defaultValue = "";
          node.innerHTML = ""; node.hidden = true;
          updateCount(kind, kind === "variable" ? state.variables : state.secrets);
        });
        for (const [kind, rows] of [["variable", state.variables], ["secret", state.secrets]]) {
          rows.forEach((row) => bindRow(kind, row));
          on("#" + prefix + "add-" + kind, "click", () => {
            if (rows.filter((row) => !row.deleted).length >= 64) throw new Error("每组最多添加 64 个变量或密钥");
            const row = { id: state.nextId++, name: "", value: "", originalName: null };
            rows.push(row);
            $("#" + prefix + (kind === "variable" ? "variables" : "secrets")).insertAdjacentHTML("beforeend", rowHTML(prefix, kind, row));
            bindRow(kind, row);
            updateCount(kind, rows);
            $(`#${prefix}${kind}-${row.id}-name`).focus();
          });
        }
      };
      const capture = (spec, prefix) => Object.fromEntries(definitions(spec).flatMap((definition, index) => {
        const value = $("#" + prefix + index)?.value || "";
        if (definition.type === "select") {
          const options = choices(definition);
          if (value && !options.some((option) => option.value === value)) throw new Error(tr("请选择处理器声明的有效选项：", "Choose a valid processor option: ") + label(definition.label, definition.key));
          if (!value && definition.secret === false && definition.required) throw new Error(tr("请选择必填选项：", "Choose the required option: ") + label(definition.label, definition.key));
        }
        return definition.secret === false || value !== "" ? [[definition.key, value]] : [];
      }));
      const captureWorkflow = (prefix) => {
        const state = drafts.get(prefix), workflow = { variables: {}, secrets: {}, runtime: {}, delete_variables: [], delete_secrets: [] };
        const names = new Set();
        let totalBytes = 0;
        for (const [kind, rows] of [["variable", state.variables], ["secret", state.secrets]]) {
          const values = kind === "variable" ? workflow.variables : workflow.secrets, deletions = kind === "variable" ? workflow.delete_variables : workflow.delete_secrets;
          for (const row of rows) {
            if (row.deleted) { if (row.originalName) deletions.push(row.originalName); continue; }
            const name = $(`#${prefix}${kind}-${row.id}-name`).value.trim(), value = $(`#${prefix}${kind}-${row.id}-value`).value;
            if (!name && !value && !row.originalName) continue;
            if (!validName(name)) throw new Error("变量或密钥名称须以大写字母开头，最多 64 字符，不能使用 EXTORE_ 前缀");
            if (kind === "secret" && !row.originalName && !value) throw new Error("新密钥必须填写值；已有密钥留空才会保留");
            if (names.has(name)) throw new Error("变量与密钥名称不能重复");
            names.add(name);
            const bytes = new TextEncoder().encode(value).length;
            if (bytes > 8192) throw new Error("每个变量或密钥的值最多 8 KiB");
            totalBytes += bytes;
            if (kind === "secret" && row.originalName && name !== row.originalName) throw new Error("密钥改名请删除旧密钥，再添加新名称和值");
            if (kind === "secret" && !value) continue;
            values[name] = value;
            if (row.originalName && name !== row.originalName) deletions.push(row.originalName);
          }
        }
        if (totalBytes > 65536) throw new Error("变量和密钥的值合计最多 64 KiB");
        workflow.delete_variables = [...new Set(workflow.delete_variables)].filter((name) => !Object.hasOwn(workflow.variables, name));
        workflow.delete_secrets = [...new Set(workflow.delete_secrets)].filter((name) => !Object.hasOwn(workflow.secrets, name));
        for (const limit of limits) {
          const text = $("#" + prefix + "runtime-" + limit.key).value;
          if (!/^[0-9]+$/.test(text)) throw new Error(limit.name + "必须是整数");
          const value = Number(text);
          if (!Number.isSafeInteger(value) || value < limit.min || value > limit.max) throw new Error(`${limit.name}必须在 ${limit.min}–${limit.max} 之间`);
          workflow.runtime[limit.key] = value;
        }
        return workflow;
      };
      page("商品处理器配置", `<p class="caption">按店铺隔离的工作流配置：处理器参数、普通变量、密钥与运行限制。保存会创建新版本；商品需重新绑定才影响之后发行的卡密，已发行卡密继续使用原来的冻结版本。</p><div id="profile-processor-contribution"></div><section class="panel processor-config-card"><h3>新建配置</h3><form id="profile-create-form" class="processor-config-form">${rootScope(auth) ? `<div class="field"><label for="profile-shop">所属店铺</label><select id="profile-shop"><option value="">请选择店铺</option>${shopsList.map((shop) => `<option value="${esc(shop.id)}" ${profileShop === shop.id ? "selected" : ""}>${esc(shop.name)}</option>`).join("")}</select></div>` : ""}<div class="processor-config-basics grid">${field("profile-name", "配置名称", "text", 'required maxlength="120"')}<div class="field"><label for="profile-processor">商品处理器</label><select id="profile-processor" required><option value="">请选择处理器</option>${specs.map((spec) => `<option value="${esc(spec.id)}">${esc(label(spec.name, spec.id))}</option>`).join("")}</select></div></div><div id="profile-fields"></div><div class="actions"><button type="submit">创建配置</button>${typeof copyProcessorPrompt === "function" ? '<button id="profile-copy-template" type="button" class="secondary">复制配置提示词</button>' : ""}</div></form></section><div id="processor-cli-prompt"></div><div id="profile-items">${items.map((item) => `<section class="panel processor-config-card"><div class="section-head"><div><h3>${esc(item.name)}</h3><p class="caption">${esc(label(specs.find((spec) => spec.id === item.processor_id)?.name, item.processor_id))} · 当前版本 ${esc(item.revision)}${item.disabled ? " · 已停用" : ""}</p><p class="caption mono">${esc(item.processor_id)} · ${esc(item.shop_id)}</p></div><div class="actions">${typeof copyProcessorPrompt === "function" ? `<button data-profile-copy="${esc(item.id)}" class="secondary" type="button">复制配置提示词</button>` : ""}${item.disabled ? "" : `<button data-profile-edit="${esc(item.id)}" class="secondary">编辑配置</button><button data-profile-delete="${esc(item.id)}" class="danger">停用</button>`}</div></div><p class="caption">已绑定商品保留各自的绑定版本；在商品页重新绑定后，后续发行才使用此配置版本。已发行卡密不会改变。</p><div id="profile-editor-${esc(item.id)}"></div></section>`).join("")}${items.length ? "" : `<div class="empty"><h3>${tr("尚未创建处理器配置", "No processor configurations yet")}</h3><p>${tr("先选择处理器并创建配置，再到商品页绑定。没有合适的处理器时，可以从上方入口通过 PR 贡献。", "Choose a processor and create a configuration, then bind it on the product page. If none fits, use the contribution entry above to submit a PR.")}</p></div>`}</div>`);
      const displayVersion = version;
      contributionView = window.ExtoreProcessorContribution?.mount({
        root: $("#profile-processor-contribution"), api: request,
        language, isCurrent: () => active() && version === displayVersion,
        identity: () => JSON.stringify([auth.role, auth.shop_id, auth.session_id, profileShop]),
      });
      const copyPrompt = (item, processorId) => {
        if (!profileShop) throw new Error("请选择所属店铺");
        if (!processorId) throw new Error("请选择可用的商品处理器");
        return copyProcessorPrompt({ shopId: profileShop, ...(item ? { profileId: item.id } : {}), processorId }, $("#processor-cli-prompt"), () => active() && version === displayVersion);
      };
      on("#profile-copy-template", "click", () => copyPrompt(null, specs.find((spec) => spec.id === $("#profile-processor").value)?.id));
      root.querySelectorAll("[data-profile-copy]").forEach((node) => node.addEventListener("click", () => run(() => { const item = items.find((profile) => profile.id === node.dataset.profileCopy); return copyPrompt(item, item.processor_id); }, node)));
      on("#profile-shop", "change", async () => { selectedProfileShop = $("#profile-shop").value; await profiles(); });
      on("#profile-processor", "change", () => { clearSecrets(); $("#profile-fields").innerHTML = editorHTML(specs.find((spec) => spec.id === $("#profile-processor").value), "profile-field-"); wireWorkflow("profile-field-"); });
      on("#profile-create-form", "submit", async () => {
        const spec = specs.find((spec) => spec.id === $("#profile-processor").value);
        if (!spec) throw new Error("请选择可用的商品处理器");
        const body = { name: $("#profile-name").value.trim(), processor_id: spec.id, configuration: capture(spec, "profile-field-"), workflow: captureWorkflow("profile-field-") };
        delete body.workflow.delete_variables; delete body.workflow.delete_secrets;
        if (rootScope(auth)) { body.shop_id = $("#profile-shop").value; if (!body.shop_id) throw new Error("请选择所属店铺"); }
        clearSecrets();
        fresh(async () => { try { await request("/admin/processor-profiles", body, "POST"); } finally { body.configuration = {}; delete body.workflow; } if (active()) await profiles(); }, "确认创建处理器配置");
      });
      root.querySelectorAll("[data-profile-edit]").forEach((node) => node.addEventListener("click", () => run(() => {
        const item = items.find((p) => p.id === node.dataset.profileEdit), spec = specs.find((s) => s.id === item.processor_id), prefix = "profile-update-" + item.id + "-";
        $("#profile-editor-" + item.id).innerHTML = `<form id="profile-update-${esc(item.id)}" class="processor-config-form">${field(prefix + "name", "配置名称", "text", 'required maxlength="120"', item.name)}${editorHTML(spec, prefix, item)}<p class="caption">普通文本和变量可直接编辑；清空可选文本会删除原内容。密钥留空保留，删除须明确点击对应删除按钮后保存。</p><button type="submit">保存新版本</button></form>`;
        wireWorkflow(prefix);
        on("#profile-update-" + item.id, "submit", async () => {
          const body = { name: $("#" + prefix + "name").value.trim(), workflow: captureWorkflow(prefix) }, configuration = capture(spec, prefix);
          if (Object.keys(configuration).length) body.configuration = configuration;
          clearSecrets();
          fresh(async () => { try { await request("/admin/processor-profiles/" + encodeURIComponent(item.id), body, "PUT"); } finally { delete body.configuration; delete body.workflow; } if (active()) await profiles(); }, "确认更新处理器配置");
        });
      }, node)));
      root.querySelectorAll("[data-profile-delete]").forEach((node) => node.addEventListener("click", () => run(() => fresh(async () => { await request("/admin/processor-profiles/" + encodeURIComponent(node.dataset.profileDelete), null, "DELETE"); if (active()) await profiles(); }, "确认停用处理器配置"), node)));
    }
    const instance = Object.freeze({ dispose() { disposed = true; version++; controller.abort(); clearContribution(); clearTotpSetup(); clearSecrets(); window.removeEventListener?.("pagehide", leavePage); if (mounts.get(root) === instance) mounts.delete(root); }, confirmFresh: fresh, get active() { return active(); } });
    mounts.set(root, instance);
    const ready = mode === "confirm" ? () => page("确认身份", "") : mode === "security" ? security : mode === "shops" ? shops : mode === "mail" ? mail : mode === "profiles" ? profiles : mode === "login" ? login : emailFlow;
    instanceReady(ready);
    async function instanceReady(handler) { try { await handler(); } catch (e) { if (active()) { if (!root.querySelector("#account-error")) page("账户", ""); error(e); } } }
    return instance;
  }
  window.ExtoreAccount = Object.freeze({ mount, route, rootScope, shopScope, sameScope });
})();
