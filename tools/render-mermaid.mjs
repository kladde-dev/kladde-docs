// Render mermaid sources to SVG without a browser.
//
// `beautiful-mermaid` reimplements mermaid's layout on top of elkjs and has no
// DOM dependency, which is the whole reason it is used here: the usual route
// (`@mermaid-js/mermaid-cli`) needs headless Chrome, about 340 MB, to measure
// text. The cost is that it is a reimplementation, so complex diagrams may not
// render identically to the mermaid that Quartz and Obsidian use.
//
// Reads {"sources": [...]} on stdin, writes {"svgs": [...]} on stdout. One
// process for the whole document -- node's startup dominates otherwise.

import { renderMermaidSVGAsync } from "beautiful-mermaid";

// Monochrome, and matching the PDF's own sans face. `transparent` keeps the
// page colour showing through rather than painting a white rectangle.
const THEME = {
  bg: "#FFFFFF",
  fg: "#000000",
  line: "#000000",
  accent: "#000000",
  muted: "#444444",
  surface: "#FFFFFF",
  border: "#000000",
  font: "TeX Gyre Heros",
  transparent: true,
};

const input = await new Promise((resolve) => {
  let buf = "";
  process.stdin.on("data", (d) => (buf += d));
  process.stdin.on("end", () => resolve(buf));
});

const { sources } = JSON.parse(input);
const svgs = [];
for (const [i, src] of sources.entries()) {
  try {
    svgs.push(await renderMermaidSVGAsync(src, THEME));
  } catch (e) {
    process.stderr.write(`mermaid diagram ${i + 1}: ${e.message}\n`);
    svgs.push(null);
  }
}
process.stdout.write(JSON.stringify({ svgs }));
