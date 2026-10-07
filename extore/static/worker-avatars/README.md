# Worker avatars

Original cartoon avatars for Extore's worker and progress views. These are decorative type hints, not verified identities. The UI must display a separate name and status; an avatar does not establish who controls a worker.

| Public path | Character |
| --- | --- |
| `/static/worker-avatars/dots.webp` | Helpful mint dot-face robot |
| `/static/worker-avatars/grok-bot.webp` | Friendly graphite cosmic robot; no vendor branding |
| `/static/worker-avatars/other.webp` | Warm amber generic robot; fallback for unknown types |
| `/static/worker-avatars/human.webp` | Generic shopworker |
| `/static/worker-avatars/processor.webp` | Compact mint automation robot |

All five assets are 256 × 256, single-frame lossless WebP, with genuine RGBA transparency. They are intended for approximately 48px avatar slots. No remote image URLs are needed.

## Provenance and encoding

Generated on 2026-10-07 using the built-in `image_gen.imagegen` tool in five separate generation calls, one per asset. Each call used `transparent_background: true`, with no reference images. No CLI/API fallback, vector substitute, or handwritten raster artwork was used.

Original PNGs remain in the generation tool's default `generated_images` directory. The selected images were only resized and encoded with ImageMagick; the generated artwork and transparency were preserved:

```sh
magick ORIGINAL.png -resize 256x256 -strip -define webp:lossless=true ASSET.webp
```

Inspected each selected avatar and encoded output. Confirmed 256 × 256 dimensions, RGBA channels, transparent corner pixels, and alpha ranging from 0 to 1. Source image identifiers and the exact prompts used follow.

## dots

Source PNG: `exec-2af2443e-830a-491a-b0a9-d191eab16212.png`

```text
Use case: stylized-concept
Asset type: small transparent cartoon worker avatar for Extore, a gray-black and mint torn-paper web app
Primary request: one original helpful robot character, named dots only for our file naming; do not draw any name.
Subject: a friendly mint robot head and shoulders, rounded square mint head, graphite face panel with two large mint dot eyes and a small welcoming mouth, three subtle round indicator dots across the upper forehead. Simple compact mint shoulders.
Style/medium: flat editorial cartoon raster illustration, bold clean graphite outlines, two or three solid color areas, a little soft illustrated shading, charming restrained expression, cohesive professional product avatar.
Composition/framing: centered front-facing bust on a square canvas, fill about 80 percent of the square, entire head and shoulders inside frame with generous transparent margins. Strong readable silhouette at 48 pixels.
Color palette: muted mint #9bc7b0, pale mint #dbe8e0, graphite #303333. Subject edge contrast must work against near-black and white backgrounds.
Scene/backdrop: genuine transparent background, isolated character only, no painted checkerboard, no background plate or circle.
Constraints: exactly one robot bust, no text, letters, numbers, vendor logos, watermark, badge, scenery, tiny fine detail, or realistic metallic rendering. Original character, not an existing mascot.
```

## grok-bot

Source PNG: `exec-9befb56c-53e0-4bd2-b156-af99ccbba4e3.png`

```text
Use case: stylized-concept
Asset type: small transparent cartoon worker avatar for Extore, a gray-black and mint torn-paper web app
Style/medium: flat editorial cartoon raster illustration, bold clean graphite outlines, two or three solid color areas, a little soft illustrated shading, charming restrained expression, cohesive professional product avatar.
Composition/framing: centered front-facing head-and-shoulders bust on a square canvas, fill about 80 percent of the square, entire head and shoulders inside frame with generous transparent margins. Strong readable silhouette at 48 pixels, oversized head and compact shoulders.
Scene/backdrop: genuine transparent background, isolated character only, no painted checkerboard, no background plate or circle.
Constraints: exactly one character bust, no text, letters, numbers, vendor logos, watermark, badge, scenery, tiny fine detail, or photorealism. Original character, not an existing mascot. Subject edge contrast must work against near-black and white backgrounds.
Primary request: one friendly original cosmic robot character, graphite in color, no vendor branding.
Subject: a graphite robot with a slightly angular rounded head, dark face panel, two large soft pale-mint glowing eyes, welcoming smile, a small simple pale-mint four-point star detail on its forehead to suggest outer space. Compact graphite shoulders with subtle mint seams.
Color palette: graphite #303333, medium gray #626d68, muted mint #9bc7b0, pale mint #dbe8e0. Avoid a black-on-black silhouette; include a soft light-gray outer edge.
```

## other

Source PNG: `exec-1ce56c89-1871-4d54-80f2-4c4e41231953.png`

```text
Use case: stylized-concept
Asset type: small transparent cartoon worker avatar for Extore, a gray-black and mint torn-paper web app
Style/medium: flat editorial cartoon raster illustration, bold clean graphite outlines, two or three solid color areas, a little soft illustrated shading, charming restrained expression, cohesive professional product avatar.
Composition/framing: centered front-facing head-and-shoulders bust on a square canvas, fill about 80 percent of the square, entire head and shoulders inside frame with generous transparent margins. Strong readable silhouette at 48 pixels, oversized head and compact shoulders.
Scene/backdrop: genuine transparent background, isolated character only, no painted checkerboard, no background plate or circle.
Constraints: exactly one character bust, no text, letters, numbers, vendor logos, watermark, badge, scenery, tiny fine detail, or photorealism. Original character, not an existing mascot. Subject edge contrast must work against near-black and white backgrounds.
Primary request: one friendly original generic custom robot character distinguished by warm amber colors.
Subject: a warmly smiling rounded amber robot head, graphite face panel with two large amber dot eyes and a small welcoming mouth, short side ear caps, compact amber shoulders. Different rounded silhouette from a square mint robot, simple and approachable.
Color palette: muted warm amber #d7aa62, cream #f2dfbf, graphite #303333. Restrained warm tone, no neon.
```

## human

Source PNG: `exec-1103e0ea-6fe0-4616-b9e2-cad68c94d718.png`

```text
Use case: stylized-concept
Asset type: small transparent cartoon worker avatar for Extore, a gray-black and mint torn-paper web app
Style/medium: flat editorial cartoon raster illustration, bold clean graphite outlines, two or three solid color areas, a little soft illustrated shading, charming restrained expression, cohesive professional product avatar.
Composition/framing: centered front-facing head-and-shoulders bust on a square canvas, fill about 80 percent of the square, entire head and shoulders inside frame with generous transparent margins. Strong readable silhouette at 48 pixels, oversized head and compact shoulders.
Scene/backdrop: genuine transparent background, isolated character only, no painted checkerboard, no background plate or circle.
Constraints: exactly one character bust, no text, letters, numbers, vendor logos, watermark, badge, scenery, tiny fine detail, or photorealism. Original character, not an existing mascot. Subject edge contrast must work against near-black and white backgrounds.
Primary request: one friendly original generic human shopworker character, not any real person.
Subject: an approachable gender-neutral adult shopworker, warm medium skin tone, short dark hair, simple gentle eyes and small friendly smile, wearing a muted mint shirt and graphite apron with no writing or logo. Head and compact shoulders only, no objects or hands.
Color palette: muted mint #9bc7b0, graphite #303333, warm medium skin #c99475, soft cream highlights. Simplified features readable at small size.
```

## processor

Source PNG: `exec-d1183e6c-811b-4679-ba97-276bc30299c7.png`

```text
Use case: stylized-concept
Asset type: small transparent cartoon worker avatar for Extore, a gray-black and mint torn-paper web app
Style/medium: flat editorial cartoon raster illustration, bold clean graphite outlines, two or three solid color areas, a little soft illustrated shading, charming restrained expression, cohesive professional product avatar.
Composition/framing: centered front-facing head-and-shoulders bust on a square canvas, fill about 80 percent of the square, entire head and shoulders inside frame with generous transparent margins. Strong readable silhouette at 48 pixels, oversized head and compact shoulders.
Scene/backdrop: genuine transparent background, isolated character only, no painted checkerboard, no background plate or circle.
Constraints: exactly one character bust, no text, letters, numbers, vendor logos, watermark, badge, scenery, tiny fine detail, or photorealism. Original character, not an existing mascot. Subject edge contrast must work against near-black and white backgrounds.
Primary request: one friendly original small automation robot, simple practical character.
Subject: a compact boxy mint automation robot head with softly rounded corners, a single broad graphite visor containing three large mint round indicator lights, tiny friendly curved mouth below the visor, one short broad antenna centered on top, squat graphite-and-mint shoulders. Distinct from a rounded dot-eye robot, no mechanical tools or extra objects.
Color palette: muted mint #9bc7b0, pale mint #dbe8e0, graphite #303333, medium gray #626d68. Simple bold shapes.
```
