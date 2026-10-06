# Vendored QR encoder

`qrcodegen.js` is the unmodified ES6 build of Project Nayuki's QR Code generator,
version **1.8.0**, distributed under the MIT license in `qrcodegen.LICENSE` and
its source header.

- Official project: <https://github.com/nayuki/QR-Code-generator>
- Official release asset: <https://github.com/nayuki/QR-Code-generator/releases/download/v1.8.0/qrcodegen-v1.8.0-es6.js>
- Tagged source commit: `720f62bddb7226106071d4728c292cb1df519ceb`
- Upstream TypeScript: <https://github.com/nayuki/QR-Code-generator/blob/v1.8.0/typescript-javascript/qrcodegen.ts>
- Asset SHA-256: `6a1116192ed1dd67fa1bf31e77f5817103d71c23bbac24c382e698b7668bdd01`

Extore serves this file from its own static assets before `totp-qr.js`. Both
ship inside the Python package; there is no runtime CDN, network request,
external QR service, or JavaScript evaluation. The wrapper constructs a white
background and a black numeric SVG path with a four-module quiet zone. It never
puts the enrollment URI into SVG attributes or text, and throws on invalid or
oversized input so the account screen can offer manual enrollment.

Non-ASCII enrollment labels are encoded as UTF-8 with ECI assignment 26. QR
images are credential material; do not log, persist, upload, or include real
enrollment images in screenshots. The tests use synthetic secrets only.
