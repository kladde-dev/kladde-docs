#!/usr/bin/env node
// Make `beautiful-mermaid` lay cyclic diagrams out the way real mermaid does.
//
// The library hands elk a layered graph but never sets
// `elk.layered.cycleBreaking.strategy`, so elk falls back to GREEDY. On a
// state diagram whose every child also points back at its parent -- three
// antiparallel edge pairs in spec/transactions-and-batches.md -- GREEDY
// reverses the wrong set: the root lands at the bottom right, the next level
// above it, and the level below that back at the bottom. Obsidian and Quartz
// use real mermaid, which uses dagre, and get it right, so the PDF was the
// only renderer that disagreed.
//
// Any non-default strategy fixes it, and all four produce identical output
// here; MODEL_ORDER is chosen because it matches the model-order handling the
// library already asks for on the line above.
//
// This has to be a patch rather than a call: `RenderOptions` exposes colours,
// fonts and spacing only, and `elkLayoutSync` bypasses ELK's public `layout()`
// to dispatch straight into an internal worker whose `saveDispatch` is an own
// property, so there is no runtime hook to wrap from tools/render-mermaid.mjs.
//
// Run by `npm ci` or `npm install` in tools/, via the `postinstall` script.
// Idempotent, and loud if the anchor ever moves -- an upgrade that silently
// skipped this would bring the bad layout back with no other symptom.

import { readFileSync, writeFileSync } from "node:fs";

const DIST = new URL(
  "./node_modules/beautiful-mermaid/dist/index.js",
  import.meta.url,
);
const ANCHOR = '"elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES"';
const ADDED = '"elk.layered.cycleBreaking.strategy": "MODEL_ORDER"';

let source;
try {
  source = readFileSync(DIST, "utf8");
} catch {
  // `npm install` runs postinstall even when the package is absent (a
  // dependency-free checkout, or --omit=optional); nothing to do.
  console.log("patch-mermaid-layout: beautiful-mermaid not installed, skipping");
  process.exit(0);
}

if (source.includes(ADDED)) {
  console.log("patch-mermaid-layout: already applied");
  process.exit(0);
}

const at = source.indexOf(ANCHOR);
if (at === -1) {
  console.error(
    "patch-mermaid-layout: could not find\n  " + ANCHOR +
    "\nin beautiful-mermaid. It has probably been upgraded. Check whether it\n" +
    "now sets elk.layered.cycleBreaking.strategy itself -- if it does, drop\n" +
    "this script and the postinstall hook; if not, re-point the anchor.",
  );
  process.exit(1);
}

writeFileSync(
  DIST,
  source.slice(0, at + ANCHOR.length) + ",\n    " + ADDED +
    source.slice(at + ANCHOR.length),
);
console.log("patch-mermaid-layout: applied");
