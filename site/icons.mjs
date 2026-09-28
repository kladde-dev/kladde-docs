// Put the logo where Quartz takes its branding from, in place of Quartz's own.
//
//   node site/icons.mjs <quartz checkout> <logo.svg>
//
// Quartz serves quartz/static/, and reads icon.png there for the favicon,
// favicon.ico, and the icon on each page's social-preview image; og-image.png
// is the preview of last resort.  Both are rendered from the SVG, so the logo
// has one source.  sharp comes with Quartz, so it is loaded from there.

import { copyFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const [quartz, logo] = process.argv.slice(2);
const sharp = createRequire(join(quartz, "package.json"))("sharp");
const statics = join(quartz, "quartz", "static");

// Rasterized far above the target size and scaled down, so edges stay crisp.
const render = (px) => sharp(logo, { density: 1200 }).resize(px, px).png().toBuffer();

copyFileSync(logo, join(statics, "logo.svg"));
await sharp(await render(512)).toFile(join(statics, "icon.png"));
await sharp({ create: { width: 1200, height: 675, channels: 4, background: "#fbf8f3" } })
  .composite([{ input: await render(360), gravity: "centre" }])
  .png()
  .toFile(join(statics, "og-image.png"));
