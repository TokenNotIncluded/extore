(function (global) {
  "use strict";

  function createSvg(uri) {
    if (typeof uri !== "string" || !uri || uri.length > 4096 || /[\u0000-\u001f\u007f]/.test(uri)) {
      throw new Error("Invalid 2FA setup URI");
    }
    let parsed;
    try { parsed = new URL(uri); } catch { throw new Error("Invalid 2FA setup URI"); }
    if (parsed.protocol !== "otpauth:" || parsed.hostname !== "totp" || parsed.pathname.length < 2 || !parsed.searchParams.get("secret")) {
      throw new Error("Invalid 2FA setup URI");
    }
    const encoder = global.qrcodegen;
    if (!encoder?.QrCode || !encoder?.QrSegment) throw new Error("2FA QR encoder unavailable");

    // Encoding never calls a network service. ECI declares UTF-8 for Unicode labels.
    const segments = encoder.QrSegment.makeSegments(uri);
    if (/[^\x00-\x7f]/.test(uri)) segments.unshift(encoder.QrSegment.makeEci(26));
    const qr = encoder.QrCode.encodeSegments(segments, encoder.QrCode.Ecc.MEDIUM);
    const size = qr.size;
    if (!Number.isInteger(size) || size < 21 || size > 177 || size % 4 !== 1 || typeof qr.getModule !== "function") {
      throw new Error("Invalid 2FA QR modules");
    }
    const border = 4;
    const paths = [];
    for (let y = 0; y < size; y++) {
      let start = -1;
      for (let x = 0; x <= size; x++) {
        const dark = x < size ? qr.getModule(x, y) : false;
        if (typeof dark !== "boolean") throw new Error("Invalid 2FA QR modules");
        if (dark && start < 0) start = x;
        if (!dark && start >= 0) {
          paths.push(`M${start + border},${y + border}h${x - start}v1H${start + border}z`);
          start = -1;
        }
      }
    }
    const extent = size + border * 2;
    // Only numeric geometry is interpolated: no URI, secret, label, or HTML text.
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${extent} ${extent}" width="256" height="256" role="img" aria-label="2FA setup QR code" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/><path d="${paths.join("")}" fill="#000"/></svg>`;
  }

  global.ExtoreTotpQr = Object.freeze({ createSvg });
})(window);
