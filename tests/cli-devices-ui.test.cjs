const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush } = require("./app-fixture.cjs");

const device = {
  id: "device-A", client_name: '<img src=x onerror="x">', fingerprint: "sha256:fingerprint",
  product_id: "product", product_name: "Product", link_name: "Scoped link",
  created: 2000000000, last_seen: 2000000001, active: true, revoked: false,
};
async function devicePage(role = "admin") {
  const page = appFixture();
  page.navigate(role === "admin" ? "/admin" : "/staff");
  page.context.acceptAuth({ role, permissions: [] });
  page.set({ tab: "sessions" });
  const button = page.node("device-revoke-button");
  button.dataset = { revokeDevice: device.id };
  page.collections.set("[data-revoke-device]", [button]);
  const rendering = page.context.renderSessions();
  const prefix = role === "admin" ? "/admin" : "/manage";
  assert.equal(page.requests[2].url, "/api" + prefix + "/cli-devices");
  page.requests[0].respond([]);
  page.requests[1].respond([{ created: 2000000000, action: "cli.device.bind", actor: "link-A", target: device.id, channel: "cli", client_name: device.client_name, fingerprint: device.fingerprint }]);
  page.requests[2].respond([device]);
  await rendering;
  return { page, button, prefix };
}

test("owner and delegated sessions render scoped CLI devices and escaped audit metadata", async () => {
  for (const role of ["admin", "staff"]) {
    const { page } = await devicePage(role);
    const markup = page.node("#workspace").innerHTML;
    assert.match(markup, /CLI 设备/);
    assert.match(markup, /sha256:fingerprint/);
    assert.match(markup, /data-revoke-device="device-A"/);
    assert.match(markup, /&lt;img src=x onerror=&quot;x&quot;&gt;/);
    assert.doesNotMatch(markup, /<img/);
  }
});

test("device revocation requires confirmation, uses the proper scoped endpoint and refreshes", async () => {
  for (const role of ["admin", "staff"]) {
    const { page, button, prefix } = await devicePage(role);
    let confirmations = 0;
    page.context.confirm = () => { confirmations++; return false; };
    await button.emit("click");
    assert.equal(confirmations, 1);
    assert.equal(page.requests.length, 3);
    page.context.confirm = () => true;
    const revoking = button.emit("click");
    assert.equal(page.requests[3].url, "/api" + prefix + "/cli-devices/device-A");
    assert.equal(page.requests[3].options.method, "DELETE");
    page.requests[3].respond({ ok: true, revoked_sessions: 1 });
    await flush();
    page.requests[4].respond([]);
    page.requests[5].respond([]);
    page.requests[6].respond([{ ...device, revoked: true, active: false }]);
    await revoking;
    const markup = page.node("#workspace").innerHTML;
    assert.match(markup, /已撤销/);
    assert.doesNotMatch(markup, /data-revoke-device="device-A"/);
  }
});

test("a stale device revocation response cannot refresh a different management page", async () => {
  const { page, button } = await devicePage();
  const revoking = button.emit("click");
  page.navigate("/staff");
  page.node("#workspace").innerHTML = "Different page";
  page.requests[3].respond({ ok: true });
  await revoking;
  assert.equal(page.requests.length, 4);
  assert.equal(page.node("#workspace").innerHTML, "Different page");
});
