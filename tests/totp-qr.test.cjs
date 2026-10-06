const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const os = require("node:os");
const { execFileSync, spawnSync } = require("node:child_process");

const vendor = fs.readFileSync(path.join(__dirname, "../extore/static/vendor/qrcodegen.js"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../extore/static/totp-qr.js"), "utf8");
const secret = "JBSWY3DPEHPK3PXP"; // Synthetic enrollment data, never an account secret.
const uri = `otpauth://totp/Extore:owner@example.test?secret=${secret}&issuer=Extore&digits=6&period=30`;

function fixture({ loadEncoder = true } = {}) {
  const network = () => { throw new Error("QR encoding must stay local"); };
  const sandbox = { URL, fetch: network, XMLHttpRequest: network, WebSocket: network };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });
  if (loadEncoder) vm.runInContext(vendor, context);
  vm.runInContext(source, context);
  return { context, createSvg: context.ExtoreTotpQr.createSvg };
}

function raster(svg, scale = 6) {
  const extent = Number(svg.match(/viewBox="0 0 (\d+) \d+"/)[1]);
  const values = new Uint8Array(extent * extent).fill(255);
  const geometry = svg.match(/<path d="([^"]*)"/)[1];
  const runs = [...geometry.matchAll(/M(\d+),(\d+)h(\d+)v1H(\d+)z/g)];
  assert.equal(runs.map((entry) => entry[0]).join(""), geometry);
  for (const [, sx, sy, sw, send] of runs) {
    const x = Number(sx), y = Number(sy), width = Number(sw);
    assert.equal(Number(send), x);
    assert.ok(width > 0 && x >= 4 && x + width <= extent - 4);
    assert.ok(y >= 4 && y < extent - 4);
    for (let i = x; i < x + width; i++) values[y * extent + i] = 0;
  }
  const pixels = Buffer.alloc(extent * extent * scale * scale, 255);
  for (let y = 0; y < extent * scale; y++) {
    for (let x = 0; x < extent * scale; x++) {
      pixels[y * extent * scale + x] = values[Math.floor(y / scale) * extent + Math.floor(x / scale)];
    }
  }
  return { extent, values, pgm: Buffer.concat([Buffer.from(`P5\n${extent * scale} ${extent * scale}\n255\n`), pixels]) };
}

test("vendored encoder retains the reviewed official release and license", () => {
  assert.equal(crypto.createHash("sha256").update(vendor).digest("hex"), "6a1116192ed1dd67fa1bf31e77f5817103d71c23bbac24c382e698b7668bdd01");
  const license = fs.readFileSync(path.join(__dirname, "../extore/static/vendor/qrcodegen.LICENSE"), "utf8");
  assert.match(license, /Copyright \(c\) Project Nayuki/);
  assert.match(license, /Permission is hereby granted, free of charge/);
});

test("local QR SVG contains only numeric geometry and preserves four-module white borders", () => {
  const { createSvg } = fixture();
  const svg = createSvg(uri);
  assert.match(svg, /^<svg xmlns="http:\/\/www.w3.org\/2000\/svg"/);
  assert.match(svg, /width="256" height="256"/);
  assert.match(svg, /fill="#fff"/);
  assert.match(svg, /fill="#000"/);
  assert.doesNotMatch(svg, /otpauth|JBSWY3DPEHPK3PXP|owner@example|<text|<image|href=|style=|onload=|<script/);
  const { extent, values } = raster(svg);
  for (let y = 0; y < extent; y++) for (let x = 0; x < extent; x++) {
    if (x < 4 || y < 4 || x >= extent - 4 || y >= extent - 4) assert.equal(values[y * extent + x], 255);
  }
});

test("Unicode and long enrollment labels fit without entering SVG markup", () => {
  const { createSvg } = fixture();
  const longEmail = "a".repeat(64) + "@" + "b".repeat(63) + "." + "c".repeat(63) + "." + "d".repeat(50) + ".test";
  const unicode = `otpauth://totp/兑所:店主😀@example.test?secret=${secret}&issuer=兑所`;
  const longUri = `otpauth://totp/Extore:${longEmail}?secret=${secret}&issuer=Extore`;
  const normal = raster(createSvg(uri)).extent;
  assert.ok(raster(createSvg(longUri)).extent > normal);
  assert.doesNotMatch(createSvg(unicode), /兑所|店主|😀/);
  assert.doesNotMatch(createSvg(longUri), /aaaa|bbbb|secret=/);
  assert.ok(raster(createSvg(unicode)).extent > 29);
});

test("URI labels containing markup cannot add SVG attributes or elements", () => {
  const payload = '<script>alert("bad")</script><img onerror="bad">';
  const dangerous = `otpauth://totp/${payload}?secret=${secret}&issuer=${encodeURIComponent(payload)}`;
  const svg = fixture().createSvg(dangerous);
  raster(svg);
  assert.doesNotMatch(svg, /alert|bad|onerror|<script|<img|&lt;|%3C/);
  assert.equal((svg.match(/<svg\b/g) || []).length, 1);
  assert.equal((svg.match(/<path\b/g) || []).length, 1);
  assert.equal((svg.match(/<rect\b/g) || []).length, 1);
});

test("invalid or oversized input and unavailable encoder fail without echoing credentials", () => {
  const { createSvg } = fixture();
  for (const value of [undefined, null, {}, 123, "", "https://example.test/", "otpauth://hotp/test?secret=abc", "otpauth://totp/?secret=abc", "otpauth://totp/name", "otpauth://totp/name?secret=", uri + "\n", uri + "a".repeat(4097)]) {
    assert.throws(() => createSvg(value), /Invalid 2FA setup URI/);
  }
  assert.throws(() => fixture({ loadEncoder: false }).createSvg(uri), /2FA QR encoder unavailable/);
  assert.throws(() => createSvg(uri + "&issuer=" + "a".repeat(2400)), /Data too long/);
  assert.throws(() => createSvg(uri + "\ud800"), /URI malformed/);
});

test("corrupt module responses fail instead of emitting unsafe SVG geometry", () => {
  const { context, createSvg } = fixture();
  for (const modules of [{ size: "21", getModule() { return true; } }, { size: 22, getModule() { return true; } }, { size: 21 }, { size: 21, getModule() { return '<img>'; } }]) {
    context.qrcodegen.QrCode.encodeSegments = () => modules;
    assert.throws(() => createSvg(uri), /Invalid 2FA QR modules/);
  }
});

const hasZbar = spawnSync("zbarimg", ["--version"], { stdio: "ignore" }).status === 0;
test("independent QR decoder recovers complete ASCII, Unicode, and long-email enrollment URIs", { skip: !hasZbar }, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "extore-qr-test-"));
  try {
    const { createSvg } = fixture();
    const inputs = [
      uri,
      `otpauth://totp/兑所:店主😀@example.test?secret=${secret}&issuer=兑所`,
      `otpauth://totp/Extore:${"a".repeat(64)}@${"b".repeat(63)}.${"c".repeat(63)}.example.test?secret=${secret}&issuer=Extore`,
    ];
    for (let i = 0; i < inputs.length; i++) {
      const image = path.join(directory, `${i}.pgm`);
      fs.writeFileSync(image, raster(createSvg(inputs[i])).pgm, { mode: 0o600 });
      const output = execFileSync("zbarimg", ["--quiet", "--raw", image], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      assert.equal(output.replace(/\n$/, ""), inputs[i]);
    }
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
