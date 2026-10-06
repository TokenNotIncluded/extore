const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush } = require("./app-fixture.cjs");

const device = {
  id: "device-A", client_name: '<img src=x onerror="x">', fingerprint: "sha256:fingerprint",
  product_id: "product", product_name: "Product", link_name: "Scoped link",
  created: 2000000000, last_seen: 2000000001, active: true, revoked: false,
};
const ownerDevice = {
  id: "owner/A", role: "admin", scope: "shop.owner",
  client_name: '<script>owner-device</script>', fingerprint: "owner-fingerprint",
  created: 2000000000, last_seen: 2000000001, expires: 2001000000,
  active: true, revoked: false,
};
function respondDevices(page, offset, role, scoped = [device], owners = [ownerDevice]) {
  page.requests[offset].respond([]);
  page.requests[offset + 1].respond([{ created: 2000000000, action: "cli.device.bind", actor: "link-A", target: device.id, channel: "cli", client_name: device.client_name, fingerprint: device.fingerprint }]);
  page.requests[offset + 2].respond(scoped);
  if (role === "admin") page.requests[offset + 3].respond(owners);
}
async function devicePage(role = "admin") {
  const page = appFixture();
  page.navigate(role === "admin" ? "/admin" : "/staff");
  page.context.acceptAuth({ role, permissions: [] });
  page.set({ tab: "sessions" });
  const button = page.node("device-revoke-button");
  button.dataset = { revokeDevice: device.id };
  page.collections.set("[data-revoke-device]", [button]);
  const ownerButton = page.node("owner-device-revoke-button");
  ownerButton.dataset = { revokeOwnerDevice: ownerDevice.id };
  page.collections.set("[data-revoke-owner-device]", [ownerButton]);
  const rendering = page.context.renderSessions();
  const prefix = role === "admin" ? "/admin" : "/manage";
  assert.equal(page.requests[2].url, "/api" + prefix + "/cli-devices");
  if (role === "admin") assert.equal(page.requests[3].url, "/api/admin/cli-owner-devices");
  else assert.equal(page.requests.length, 3);
  respondDevices(page, 0, role);
  await rendering;
  return { page, button, ownerButton, prefix, count: role === "admin" ? 4 : 3 };
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
    const { page, button, prefix, count } = await devicePage(role);
    let confirmations = 0;
    page.context.confirm = () => { confirmations++; return false; };
    await button.emit("click");
    assert.equal(confirmations, 1);
    assert.equal(page.requests.length, count);
    page.context.confirm = () => true;
    const revoking = button.emit("click");
    assert.equal(page.requests[count].url, "/api" + prefix + "/cli-devices/device-A");
    assert.equal(page.requests[count].options.method, "DELETE");
    page.requests[count].respond({ ok: true, revoked_sessions: 1 });
    await flush();
    respondDevices(page, count + 1, role, [{ ...device, revoked: true, active: false }]);
    await revoking;
    const markup = page.node("#workspace").innerHTML;
    assert.match(markup, /已撤销/);
    assert.doesNotMatch(markup, /data-revoke-device="device-A"/);
  }
});

test("a stale device revocation response cannot refresh a different management page", async () => {
  const { page, button, count } = await devicePage();
  const revoking = button.emit("click");
  page.navigate("/staff");
  page.node("#workspace").innerHTML = "Different page";
  page.requests[count].respond({ ok: true });
  await revoking;
  assert.equal(page.requests.length, count + 1);
  assert.equal(page.node("#workspace").innerHTML, "Different page");
});

test("owner devices are separate full-shop grants and never appear in the staff panel", async () => {
  const { page } = await devicePage();
  const markup = page.node("#workspace").innerHTML;
  assert.match(markup, /商家 CLI 设备 · 全店管理/);
  assert.match(markup, /商品 CLI 设备 · 按商品授权/);
  assert.match(markup, /完整商家管理权限/);
  assert.match(markup, /owner-fingerprint/);
  assert.match(markup, /&lt;script&gt;owner-device&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script>/);
  assert.match(markup, /data-revoke-owner-device="owner\/A"/);
  const staff = await devicePage("staff");
  assert.doesNotMatch(staff.page.node("#workspace").innerHTML, /商家 CLI 设备|owner-fingerprint|copy-owner-ai/);
  await staff.ownerButton.emit("click");
  assert.equal(staff.page.requests.length, 3);
});

test("owner device revocation confirms full scope and only calls the owner endpoint", async () => {
  const { page, ownerButton, count } = await devicePage();
  let confirmation;
  page.context.confirm = (message) => { confirmation = message; return false; };
  await ownerButton.emit("click");
  assert.match(confirmation, /全店管理权限/);
  assert.equal(page.requests.length, count);
  page.context.confirm = () => true;
  const revoking = ownerButton.emit("click");
  assert.equal(page.requests[count].url, "/api/admin/cli-owner-devices/owner%2FA");
  assert.equal(page.requests[count].options.method, "DELETE");
  page.requests[count].respond({ ok: true, revoked_sessions: 2 });
  await flush();
  respondDevices(page, count + 1, "admin", [device], [{ ...ownerDevice, revoked: true, active: false }]);
  await revoking;
  assert.doesNotMatch(page.node("#workspace").innerHTML, /data-revoke-owner-device=/);
});

test("late owner revocation cannot overwrite an approval page", async () => {
  const { page, ownerButton, count } = await devicePage();
  const revoking = ownerButton.emit("click");
  page.navigate("/cli/owner#public-request");
  page.node("#workspace").innerHTML = "Approval page";
  page.requests[count].respond({ ok: true });
  await revoking;
  assert.equal(page.requests.length, count + 1);
  assert.equal(page.node("#workspace").innerHTML, "Approval page");
});
