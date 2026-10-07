(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ExtoreProxyRouting = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";
  const routeID = /^[0-9a-f]{32}$/;
  const secretPattern = /^[A-Z2-7]{32}$/;
  const candidate = /^EXR[0-9]+(?:\.|$)/i;
  const candidateLike = (code) => candidate.test(code) || (/^EXR[0-9]+/i.test(code) && (code.length > 64 || /^EXR[0-9]*[0189]/i.test(code)));
  const error = () => new Error("无法确认卡密的发行站。请核对卡密，或直接联系发行商家。 / Unable to verify the issuer. Check the code or contact the merchant.");
  let pendingCode = "";
  let pendingError = false;

  function takeFragment(location = root.location, history = root.history) {
    if (!location || !String(location.hash || "").startsWith("#extore-code=")) return "";
    const fragment = String(location.hash).slice(13);
    // Strip the fragment synchronously, before preferences, WebMCP or app fetches.
    const cleanURL = location.pathname + (location.search || "");
    if (history?.replaceState) history.replaceState({}, "", cleanURL);
    else { location.replace(cleanURL); return ""; }
    try {
      const code = decodeURIComponent(fragment);
      if (!code || code.length > 8000 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(code)) throw error();
      if (location.pathname !== "/" && location.pathname !== "/proxy") return "";
      return code;
    } catch (_) { throw error(); }
  }
  try { pendingCode = takeFragment(); } catch (_) { pendingError = true; }

  function consumeIncoming() {
    const code = pendingCode;
    pendingCode = "";
    if (pendingError) { pendingError = false; throw error(); }
    return code;
  }

  function decode(value, expected) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) throw error();
    let binary;
    try { binary = root.atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4)); }
    catch (_) { throw error(); }
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    if (bytes.length !== expected || encode(bytes) !== value) throw error();
    return bytes;
  }
  function encode(bytes) {
    return root.btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function parse(code) {
    if (typeof code !== "string" || code.trim().length > 200) throw error();
    const parts = code.trim().split(".");
    if (parts.length !== 5 || parts[0] !== "EXR1" || !routeID.test(parts[1]) || !secretPattern.test(parts[2]) || !routeID.test(parts[3])) throw error();
    decode(parts[4], 64);
    return { route_id: parts[1], secret: parts[2], issuer_id: parts[3], signature: parts[4] };
  }
  function safeRoute(value) {
    if (!value || typeof value !== "object" || !routeID.test(value.route_id) || !routeID.test(value.issuer_id) || typeof value.origin !== "string" || typeof value.path !== "string") throw error();
    let url;
    try { url = new URL(value.origin); } catch (_) { throw error(); }
    if (url.protocol !== "https:" || url.origin !== value.origin || url.username || url.password || url.search || url.hash || url.pathname !== "/" || /[\s\\%]/.test(value.origin) || !/^\/[A-Za-z0-9/_-]*$/.test(value.path) || value.path.length > 150 || value.path.includes("//")) throw error();
    const host = url.hostname.toLowerCase();
    if (!host.includes(".") || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || /^\d+(?:\.\d+)*$/.test(host) || host.includes(":") || host.split(".").some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) throw error();
    decode(value.public_key, 32);
    return { route_id: value.route_id, issuer_id: value.issuer_id, name: typeof value.name === "string" ? value.name.slice(0, 100) : url.hostname, origin: value.origin, path: value.path, public_key: value.public_key };
  }
  async function verify(code, route, crypto = root.crypto) {
    try {
      const parsed = parse(code);
      route = safeRoute(route);
      if (parsed.route_id !== route.route_id || parsed.issuer_id !== route.issuer_id || !crypto?.subtle) throw error();
      const encoder = new TextEncoder();
      const hash = await crypto.subtle.digest("SHA-256", encoder.encode(parsed.secret));
      const digest = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
      const message = encoder.encode("Extore routed code v1\n" + route.route_id + "\n" + route.issuer_id + "\n" + route.origin + "\n" + route.path + "\n" + digest);
      const key = await crypto.subtle.importKey("raw", decode(route.public_key, 32), { name: "Ed25519" }, false, ["verify"]);
      if (!await crypto.subtle.verify({ name: "Ed25519" }, key, decode(parsed.signature, 64), message)) throw error();
      return parsed.secret;
    } catch (_) { throw error(); }
  }
  async function fetchRoutes() {
    const response = await root.fetch("/api/proxy/routes", { credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer" });
    if (!response.ok) throw error();
    return response.json();
  }
  async function routeCodes(raw, options = {}) {
    if (typeof raw !== "string" || !raw.trim() || raw.length > 8000) throw error();
    const pieces = raw.trim().split(/[\s,，;；]+/).filter(Boolean);
    if (!pieces.some(candidateLike)) return { localCodes: [raw], groups: [] };
    if (pieces.length > 30) throw error();
    const metadata = await (options.fetchRoutes || fetchRoutes)();
    if (!Array.isArray(metadata) || metadata.length > 1000) throw error();
    const routes = new Map();
    for (const item of metadata) {
      const route = safeRoute(item);
      if (routes.has(route.route_id)) throw error();
      routes.set(route.route_id, route);
    }
    const origin = options.origin || root.location?.origin;
    const path = options.path || root.location?.pathname || "/";
    const groups = new Map();
    const localCodes = [];
    const seen = new Set();
    for (const code of pieces) {
      if (!candidateLike(code)) { localCodes.push(code); continue; }
      const parsed = parse(code);
      const route = routes.get(parsed.route_id);
      if (!route) throw error();
      // Even duplicate inputs are verified; a bad signature never disappears.
      await verify(code, route, options.crypto || root.crypto);
      const identity = route.route_id + ":" + parsed.secret;
      if (seen.has(identity)) continue;
      seen.add(identity);
      if (route.origin === origin && (route.path === path || route.path === "/" || route.path === "/proxy")) localCodes.push(code);
      else {
        if (!groups.has(route.route_id)) groups.set(route.route_id, { route, codes: [] });
        groups.get(route.route_id).codes.push(code);
      }
    }
    return { localCodes, groups: [...groups.values()] };
  }
  function destination(group) {
    const route = safeRoute(group.route);
    if (!Array.isArray(group.codes) || !group.codes.length || group.codes.length > 30) throw error();
    for (const code of group.codes) {
      const parsed = parse(code);
      if (parsed.route_id !== route.route_id || parsed.issuer_id !== route.issuer_id) throw error();
    }
    return route.origin + route.path + "#extore-code=" + encodeURIComponent(group.codes.join("\n"));
  }
  function publicResult(result) {
    return { routing: true, local_count: result.localCodes.length, groups: result.groups.map(({ route, codes }) => ({ route_id: route.route_id, name: route.name, origin: route.origin, path: route.path, count: codes.length })) };
  }
  function renderRoutingChoice(app, result, helpers = {}) {
    const tr = helpers.tr || ((zh) => zh);
    const esc = helpers.esc || ((value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]));
    const count = (size) => `${size} ${tr("张卡密", size === 1 ? "code" : "codes")}`;
    app.innerHTML = `<section class="narrow proxy-paper"><h1>${tr("前往发行站领取", "Continue to the issuer")}</h1><p>${tr("这些卡密有独立的领取站。选择一组继续，卡密只会发送到对应站点。", "Choose a group to continue. Its codes are sent only to that issuer.")}</p><div class="paper-divider" aria-hidden="true"></div><div class="proxy-route-list">${result.groups.map((group, index) => `<article class="proxy-route-row"><div><h2>${esc(group.route.name)}</h2><p class="proxy-route-domain">${esc(group.route.origin)}</p><p class="caption">${count(group.codes.length)} · ${tr("已确认发行站", "issuer verified")}</p></div><button type="button" data-proxy-route="${index}">${tr("前往领取", "Continue")}</button></article>`).join("")}${result.localCodes.length ? `<article class="proxy-route-row"><div><h2>${tr("在本站继续", "Continue on this site")}</h2><p class="caption">${count(result.localCodes.length)}</p></div><button type="button" id="proxy-local">${tr("继续兑换", "Continue")}</button></article>` : ""}</div><p class="caption">${tr("没有发行信息的旧卡密不会在其他站点试探验证。", "Legacy codes are never tested against other sites.")}</p><div id="proxy-error" class="error" role="alert"></div><button type="button" id="proxy-back" class="secondary">${tr("重新输入", "Enter another code")}</button></section>`;
    app.querySelectorAll("[data-proxy-route]").forEach((button) => button.addEventListener("click", () => {
      button.disabled = true;
      try { (helpers.location || root.location).replace(destination(result.groups[Number(button.dataset.proxyRoute)])); }
      catch (_) { button.disabled = false; app.querySelector("#proxy-error").textContent = error().message; }
    }));
    app.querySelector("#proxy-local")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await helpers.onLocal?.(result.localCodes.join("\n")); }
      catch (caught) {
        button.disabled = false;
        const feedback = app.querySelector("#proxy-error");
        if (feedback) feedback.textContent = caught?.message || error().message;
      }
    });
    app.querySelector("#proxy-back")?.addEventListener("click", () => helpers.onBack?.());
    app.querySelector("h1")?.setAttribute("tabindex", "-1");
    app.querySelector("h1")?.focus();
    return publicResult(result);
  }
  return Object.freeze({ parse, verify, routeCodes, safeRoute, takeFragment, consumeIncoming, destination, publicResult, renderRoutingChoice, isRoutedCode: (code) => candidateLike(String(code || "").trim()) });
});
