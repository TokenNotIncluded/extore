const test = require("node:test");
const assert = require("node:assert/strict");
const { File } = require("node:buffer");
const { appFixture, flush, product, job } = require("./app-fixture.cjs");
const merchant = { role: "admin", superadmin: false, shop_id: "shop-one", session_id: "session-one" };
const other = { ...merchant, shop_id: "shop-two", session_id: "session-two" };
const root = { role: "admin", superadmin: true, shop_id: null, session_id: "platform-session" };
const headers = (request) => request.options.headers;

test("management requests capture scope and session while anonymous endpoints remain unbound", async () => {
  const p = appFixture(); p.context.acceptAuth(merchant);
  for (const url of ["/admin/products", "/manage/jobs", "/platform/settings", "/shop/account", "/auth/totp/setup", "/auth/register/options"]) {
    const request = p.context.api(url, {}, "POST");
    assert.equal(headers(p.requests.at(-1))["X-Extore-Shop-Scope"], "shop-one");
    assert.equal(headers(p.requests.at(-1))["X-Extore-Session-ID"], "session-one");
    p.requests.at(-1).respond({}); await request;
  }
  for (const url of ["/auth/status", "/auth/email/login", "/auth/login/options", "/exchange", "/retry"]) {
    const request = p.context.api(url, {}, "POST", { expectedScope: "wrong", expectedSessionId: "wrong" });
    assert.equal(headers(p.requests.at(-1))["X-Extore-Shop-Scope"], undefined);
    assert.equal(headers(p.requests.at(-1))["X-Extore-Session-ID"], undefined);
    p.requests.at(-1).respond({}); await request;
  }
  p.context.acceptAuth(root);
  const request = p.context.api("/admin/products", undefined, "GET", { expectedScope: "shop-one", sessionId: "session-one" });
  assert.equal(headers(p.requests.at(-1))["X-Extore-Shop-Scope"], "shop-one");
  assert.equal(headers(p.requests.at(-1))["X-Extore-Session-ID"], "session-one");
  p.requests.at(-1).respond([]); await request;
  assert.equal(p.getContext().shopId, null);
  assert.equal(p.getContext().superadmin, true);
  assert.equal(p.getContext().sessionId, "platform-session");
});

test("multipart uploads preserve their invocation scope across the asynchronous limit lookup", async () => {
  const p = appFixture({ uploadLimit: null }); p.context.acceptAuth(merchant);
  const uploading = p.context.uploadMultipart("/manage/files/upload", { job_id: "job-one" }, new File(["private attachment"], "proof.txt"));
  assert.equal(p.requests[0].url, "/api/upload-limits");
  p.context.acceptAuth(other); p.requests[0].respond({ max_file_bytes: 1000 }); await flush();
  assert.equal(p.requests[1].url, "/api/manage/files/upload");
  assert.equal(headers(p.requests[1])["X-Extore-Shop-Scope"], "shop-one");
  assert.equal(headers(p.requests[1])["X-Extore-Session-ID"], "session-one");
  assert.equal(headers(p.requests[1])["Content-Type"], undefined);
  p.requests[1].respond({ id: "file" }); await uploading;
});

test("file reads use explicit session binding and stale queue results do not render another shop", async () => {
  const p = appFixture(); p.context.acceptAuth(merchant); p.navigate("/admin"); p.set({ tab: "jobs" });
  const rendering = p.context.renderJobs("", "product");
  p.context.acceptAuth(other); p.requests[0].respond([product("Old private product")]); await rendering;
  assert.equal(p.requests.length, 1);
  assert.doesNotMatch(p.node("#workspace").innerHTML, /Old private product/);
  const download = p.context.fileResponse("/manage/files/file/download", { expectedScope: "shop-one", expectedSessionId: "session-one" });
  assert.equal(headers(p.requests[1])["X-Extore-Shop-Scope"], "shop-one");
  assert.equal(headers(p.requests[1])["X-Extore-Session-ID"], "session-one");
  p.requests[1].respond({}); await download;
});

test("WebMCP UI refresh uses the current receipt without referring to start-local variables", async () => {
  const p = appFixture(); p.set({ currentToken: "receipt-token", currentProduct: product() });
  const refreshing = p.getActions().refreshUI();
  assert.equal(p.requests[0].url, "/api/receipt");
  p.requests[0].respond({ product: product(), job: job("task", "needs_input") }); await refreshing;
  assert.match(p.node("#app").innerHTML, /需要重试/);
  assert.equal(p.getActions().retryOriginal, p.context.retryOriginalReceipt);
});

async function eventsPage() {
  const p = appFixture(); p.context.acceptAuth(merchant); p.navigate("/admin"); p.set({ tab: "events" });
  let fresh;
  p.context.window.ExtoreAccount = {
    rootScope: (auth) => auth.superadmin === true && auth.shop_id === null,
    shopScope: (auth) => auth.role === "admin" && typeof auth.shop_id === "string",
    mount() { return { dispose() {}, confirmFresh(handler) { fresh = handler; } }; },
  };
  const rendering = p.context.renderEvents();
  p.requests[0].respond([{ id: 'event"><script>x</script>', type: "done", error: '<img onerror="bad">', webhook_state: "delivered", created: 1 }]);
  p.requests[1].respond({ shop_id: "shop-one", policy: { enabled: true, event_retention_days: 30, dead_letter_retention_days: 90, audit_retention_days: 180, link_retention_days: 90 } });
  await rendering;
  return { p, fresh: () => fresh };
}

test("retention submits on form submit, validates audit floor, and waits for fresh authentication", async () => {
  const { p, fresh } = await eventsPage();
  assert.doesNotMatch(p.node("#workspace").innerHTML, /<script>|<img onerror/);
  for (const [id, value] of [["events", 30], ["dead", 90], ["audit", 89], ["links", 90]]) p.node("#retention-" + id).value = value;
  await p.node("#maintenance-policy-form").emit("submit");
  assert.match(p.node("#error").textContent, /至少保留 90 天/);
  assert.equal(fresh(), undefined); assert.equal(p.requests.length, 2);
  p.node("#retention-audit").value = 180; p.node("#retention-enabled").checked = true;
  await p.node("#maintenance-policy-form").emit("submit");
  assert.equal(p.requests.length, 2); assert.equal(typeof fresh(), "function");
  p.context.acceptAuth(other); await fresh()();
  assert.equal(p.requests.length, 2, "cross-shop stepup must not execute the old policy mutation");
});

test("event cleanup previews bounded candidates and executes only after explicit fresh approval", async () => {
  const { p, fresh } = await eventsPage();
  const preview = p.node("#events-cleanup-preview").emit("click");
  assert.deepEqual(p.requests[2].body, { areas: ["events"], dry_run: true, limit: 100 });
  p.requests[2].respond({ eligible: { events: 5 } }); await preview;
  assert.match(p.node("#events-cleanup").innerHTML, /5 条/);
  await p.node("#events-cleanup-run").emit("click");
  assert.equal(p.requests.length, 3); assert.equal(typeof fresh(), "function");
  const cleanup = fresh()();
  assert.deepEqual(p.requests[3].body, { areas: ["events"], dry_run: false, limit: 100 });
  p.requests[3].respond({ changed: { events: 5 } }); await flush();
  p.requests[4].respond([]); p.requests[5].respond({ policy: {} }); await cleanup;
});

test("links default to active and the change control exposes history without archived records", async () => {
  const p = appFixture(); p.context.acceptAuth(merchant); p.navigate("/admin"); p.set({ tab: "staff", products: [product()] });
  const links = [
    { id: "active", name: "Active name", expires: Date.now() / 1000 + 1000, product_id: "product" },
    { id: "revoked", name: "Revoked name", revoked: true, expires: Date.now() / 1000 + 1000, product_id: "product" },
    { id: "archived", name: "Archived name", archived: true, revoked: true, expires: 1, product_id: "product" },
  ];
  const rendering = p.context.renderStaff(); p.requests[0].respond(links); await rendering;
  assert.equal(p.requests[0].url, "/api/admin/staff");
  assert.match(p.node("#workspace").innerHTML, /Active name/);
  assert.doesNotMatch(p.node("#workspace").innerHTML, /Revoked name|Archived name/);
  p.node("#links-view").value = "history";
  const history = p.node("#links-view").emit("change");
  assert.equal(p.requests[1].url, "/api/admin/staff?view=history");
  p.requests[1].respond(links); await history;
  assert.match(p.node("#workspace").innerHTML, /Revoked name/);
  assert.doesNotMatch(p.node("#workspace").innerHTML, /Active name|Archived name/);
  const delayed = p.context.renderStaff("all"); p.context.acceptAuth(other); p.requests[2].respond(links); await delayed;
  assert.doesNotMatch(p.node("#workspace").innerHTML, /Archived name/);
});
