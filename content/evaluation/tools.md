---
title: Benchmarks and tests
---

Every program that tests kladde-rs or measures it, in kladde-rs and in this repository: what each runs, what it records, how to run it, and how to read what it prints.
The results are on the other pages of [Evaluation](index.md); this page has none.

It describes kladde-rs at commit `7c5cdd6` on its branch `text-bench`, which is the branch `defrag-shifts` and one more benchmark, and which holds the design of [allocations and an address table](../superseded/allocations.md) that every evaluation page but the last measured; the branch `address-table`, kladde-rs's main branch at the time, lacks `text-bench` and most of what kladde-svg measures.

**kladde-rs's `main` now holds the present design, and keeps less of this.**
It has `text-bench`, and `svg-bench` with one layout, since every list and string is inline in one sequence, with the options `--quick`, `--edits`, `--only`, `--seed`, `--variant`, `--focus`, `--focus-moves`, `--journal-budget`, `--churn-floor` and `--target-fill`, and the span policy's `--span-min` and `--rewrite-below`; [the file as one sequence, measured](one-sequence.md) describes its tables.
`kladde-bench`'s synthetic scenarios, which allocated, resized and freed, went with the design they measured.
Its tests are the store's oracle, against a byte vector, its simulated power cuts and its consolidation, the containers' randomized tests, the derive's, and kladde-svg's round trip.

## At a glance

| program | where | what it does | what it leaves |
| --- | --- | --- | --- |
| [`cargo test --workspace`](#the-tests) | kladde-rs | unit, integration and documentation tests, among them a differential oracle and simulated power cuts | pass or fail |
| [`kladde-bench`](#kladde-bench-synthetic-workloads) | kladde-rs, `crates/kladde-bench` | six synthetic workloads on the store, and variants of the options | one table per workload |
| [`text-bench`](#text-bench-typing-into-one-text) | kladde-rs, `crates/kladde-bench` | typing into one long text | one table, and a summary |
| [`svg-bench`](#svg-bench-a-drawing-editor) | kladde-rs, `crates/kladde-svg` | a drawing editor's edits on an SVG drawing | one table, and a summary |
| [`svg-roundtrip`](#svg-roundtrip-a-corpus-through-kladde) | kladde-rs, `crates/kladde-svg` | every SVG file of a corpus through kladde and back, and their sizes | one table, a summary, and an exit status |
| [`plot-evaluation.py`](#the-figures) | kladde-docs, `tools/` | the figures of the evaluation pages, from the tables | SVG figures, and a summary table |
| [`simulate-ripeness.py`](#the-ripeness-simulation) | kladde-docs, `tools/` | a simulation of the ripeness drafts' estimators | tables of their costs |
| [`check-examples.py`](#checks-of-this-repository) | kladde-docs, `tools/` | the Rust examples of these pages, compiled and run against kladde-rs | pass or fail |
| [`make-pdf.py --check-only`, `check-site-links.py`](#checks-of-this-repository) | kladde-docs, `tools/` | this repository's links and page list | pass or fail |

kladde-rs runs no continuous integration; this repository's [runs on every push](#continuous-integration).

**Every benchmark is deterministic but for its times**: the same commit, options and seed make the same operations and write the same tables.
Each one checks, at the end, that the reopened file reads what the program holds in memory, and panics otherwise, so a table from a run that finished describes a file that was right.

## The tests

```sh
cargo test --workspace
```

**The suite takes about half a minute once built, in debug mode, where the store's debug assertions check its invariants as it goes**: among others, that a page a flush retires holds no live statement.
`Store::check`, which the tests call after flushes, cross-checks the fragment map, the statement slab, the pins and every page's coverage, and panics on the first disagreement.
Building with `--release` skips the assertions, so the tests are meant for debug builds.

| crate | tests | what they cover |
| --- | --- | --- |
| `kladde-store` | `tests/oracle.rs` | the differential oracle: allocations, frees, resizes, writes, splices, copies and moves, chosen at random, against a model of byte vectors; a check of every allocation's size and bytes and of `Store::check` after one step in twenty, and a reopening from the storage's bytes after one in a hundred; small and large allocations, with and without consolidation, and with a journal of one page |
| | `tests/crash.rs` | power cuts: `MemoryStorage::with_crash_tracking` fails the storage after a random number of operations, and the file as it would be with none of the writes since the last sync, all of them, or four random subsets of them, must recover onto a transaction boundary no earlier than the last completed flush |
| | `tests/consolidation.rs` | that consolidation keeps a file near its live size under overwrites, appends and a mass free, without changing what it reads |
| | `tests/churn.rs`, `tests/restate.rs`, `tests/sizes.rs` | id recycling, bytes that splices shift within an allocation, and the newest size rule, each checked in a reopened file |
| | unit tests | the CRC, the fold, loading, pages, the journal, and the encoding of statements |
| `kladde-types` | `tests/packed.rs`, `tests/small.rs` | packed vectors and small values under random edits through every mutating method they offer, against a model after every edit and a reopened file after every few |
| | the other `tests/` | the whole stack through `Kladde`, enums' `parts()`, `move_element`, schemas, tuples |
| `kladde-derive` | `tests/`, and `compile_fail` examples | what `#[derive(Persistable)]` generates, and what it must refuse to compile |
| `kladde-schema`, `kladde-persist`, `kladde-varint`, `kladde` | unit tests | descriptors, canonical encoding and fingerprints; the serialization layer; varints; the facade |
| `kladde-svg` | `tests/roundtrip.rs` | the canonical form of SVG, what the model types, and a drawing's trip through kladde and a reopening, byte for byte |
| every crate | documentation examples | that every public item's example compiles and runs |

**The randomized tests are seeded, so a failure repeats.**
They run a range of seeds each; the crash test names the failing seed in its message, and for the oracle, narrowing the range finds it.
A seed that found a defect is kept as a test of its own, as `a_grow_witness_in_a_reused_page_survives` keeps seed 229.

Some tests run only with a feature of kladde-svg, which the workspace's run leaves off:

```sh
cargo test -p kladde-svg --features f64      # decimals as f64
cargo test -p kladde-svg --features packed   # and small-ids, small-attrs, small: the layouts
```

## The tables

**`kladde-bench`, `text-bench` and `svg-bench` write the same table: a CSV file with one row per explicit flush, which `plot-evaluation.py` reads whichever program wrote it.**
A run first builds its file, which is not measured, then records a row after every interval of operations; the columns are the run's identity, a snapshot of the file, the work done since the measured phase began, and two times.

| column | meaning |
| --- | --- |
| `scenario` | the workload: a `kladde-bench` scenario, `text`, or `svg` |
| `variant` | what distinguishes runs of one workload: `on` or `off` for consolidation, the name of a variant of the options, the text's size, or the drawing |
| `size` | the workload's size in bytes: its nominal live size for `kladde-bench`, the text's size at the start for `text-bench`, and the drawing's live bytes once stored for `svg-bench` |
| `flush` | rows so far |
| `ops` | operations since the measured phase began: writes, allocations and the like for `kladde-bench`, keystrokes, or edits |
| `app_bytes` | the bytes the application wrote since then, by the program's own measure (see each program) |
| `file_pages` | the file's length, in pages of 4,096 bytes |
| `data_pages`, `table_pages` | live data pages, and live address-table pages without the headers |
| `free_pages` | pages within the file that hold nothing live: reusable, or retired by the last commit |
| `live_data`, `live_table` | the bytes of those data and table pages that current state relies on |
| `alloc_bytes`, `allocations` | the sizes of the live allocations, summed, and their number, without the store's own consolidator state |
| `statements`, `fragments` | live statements in the address table, and entries in the fragment map |
| `budget` | the consolidation budget, in pages, as the controller set it |
| `data_written`, `table_written`, `headers_written` | pages written since the measured phase began: data pages, table pages without the headers, and headers |
| `journal_bytes` | bytes appended to the journal since then |
| `fresh_bytes` | bytes of the application's content that flushes wrote into data pages |
| `evacuated_pages`, `evacuated_bytes` | data pages that consolidation evacuated, and the live bytes it wrote again to do so |
| `free_filled` | victims taken into pages the flush wrote anyway |
| `budget_pages` | data pages opened only to take victims |
| `table_rewrites` | table pages that a page rewrite drained |
| `window_restated` | fragments that the rotating window restated |
| `defrag_rewrites`, `defrag_bytes` | rewrites by description defragmentation, and the bytes they wrote |
| `compaction_flushes`, `truncations` | flushes in compaction mode, and truncations of the file |
| `flush_us` | the explicit flush that ended the interval, in microseconds, its `fsync` included |
| `ops_us` | the interval's operations, in microseconds: for `kladde-bench` the whole loop, choosing the operations and keeping the model included; for `text-bench` and `svg-bench` the time in kladde's calls alone |
| `restated_bytes`, `merged_bytes` | bytes that splices and moves shifted within their allocation, which flushes stated at their new offsets where they lie, or wrote again to merge the fragments they lay in |

### Reading a table

- **The counters of work are cumulative**: the difference between two rows is the work of the interval between them, and the last row's is the run's.
- **The bytes written** are `journal_bytes` plus 4,096 bytes for every page in `data_written`, `table_written` and `headers_written`.
  Over `app_bytes`, they are the *write amplification*; over `ops`, the bytes per operation.
- **The file's size over its live size**, `file_pages` × 4,096 over `alloc_bytes`, is the *space amplification*; the live pages, (`data_pages` + `table_pages`) × 4,096 over `alloc_bytes`, leave out the free pages, the headers and the journal.
- **A page's fill** is its live bytes over the 4,081 bytes of content it holds, so `live_data` over 4,081 × `data_pages` for the data pages, and likewise for the table.
- **The steady state** is what the evaluation pages average over the second half of a run, after the file has settled from its fill; its first rows still show the file the fill left.
- **Times** depend on the machine and vary between identical runs, so they compare only between runs on one machine, and best by their medians over rows; ratios such as bytes per operation carry over to other machines.
  The evaluation pages name the machine and how far identical runs differed on it.

## `kladde-bench`: synthetic workloads

```sh
cargo run --release -p kladde-bench -- <output directory> [scenario ...] [quick]
```

**Each scenario fills a file, then writes to it until the application has written eight times its live size, flushing after every 1000 operations, and records a row per flush in `<scenario>.csv`.**
Without a scenario, all of them run, which takes a few minutes; `quick` makes every one small.
The files go to `files/` in the output directory and are removed when the run is done.

| scenario | live size | what the application does | `app_bytes` counts | variants, one run each |
| --- | --- | --- | --- | --- |
| `uniform` | 1, 8 and 64 MiB | writes of 1 to 511 bytes at random offsets of random allocations of 16 KiB | the bytes written | consolidation `on` at each size, and `off` at 1 and 8 MiB |
| `skewed` | as above | the same, with nine writes in ten going to a tenth of the allocations | as above | as above |
| `append` | 16 MiB | 512 logs, appended to in records of 16 to 240 bytes and truncated to nothing once a record would take one past 64 KiB | the records | `on`, `off` |
| `churn` | 16 MiB | values of 16 to 128 bytes, or one in five of 1 to 8 KiB, replaced by a new allocation, rewritten whole, or patched in 1 to 64 bytes | the bytes written | `on`, `off` |
| `shrink` | 64 MiB | allocations of 64 KiB, three quarters of them freed at once, then 400 flushes of 100 patches of 1 to 64 bytes | the patches | `on`, `no-compaction`, `off` |
| `typed` | 16 MiB | a `Kladde<PersistableHashMap<u64, PersistableString>>` of strings of 50 to 500 bytes, through the typed layers: 40 % replace a string, 30 % append to one, 20 % insert one, 10 % remove one | the strings' bytes | `on` |
| `tuning` | 8 and 16 MiB | `uniform` and `skewed` at 8 MiB with a churn floor of 0.5 or 2, or a target fill of 0.6 or 0.5; `append` with 4 and 16 pages of defragmentation share | as above | one table per workload, `tuning-<workload>.csv`, with the default among the variants |

With `quick`, every live size is 1 MiB, and `shrink`'s 4 MiB.
The `shrink` scenario flushes every 100 patches rather than every 1000 operations, and its first row follows the mass free.

**The branches of the cleaning policies extend it**, and their evaluation pages say so where they use it: the ripeness branches add a scenario `mixed`, skewed overwrites of allocations that are each hot, cool or cold, and five columns after the others, `kappa`, the price of space their controller sets, and `mixed_pages`, `mixed_bytes`, `cold_bytes` and `cold_mixed_bytes`, how the three classes share data pages.
On those branches, `KLADDE_BENCH_NO_STATE` in the environment makes the stores keep no consolidator state, and `KLADDE_BENCH_MYOPIC` makes them rank pages by the myopic rule.

## `text-bench`: typing into one text

```sh
cargo run --release -p kladde-bench --bin text-bench -- <output directory> \
    [--quick] [--size BYTES] [--keystrokes N] [--seed N] [--variant NAME] \
    [--focus SHARE] [--focus-moves N] \
    [--journal-budget PAGES] [--shift-decay FACTOR] [--churn-floor λ] [--target-fill τ]
```

**A text of random words in one allocation of the store is edited keystroke by keystroke at a cursor, as a text editor would; every keystroke that changes it is a splice, which makes this the benchmark of what splices cost.**
The text is 64 KiB by default, words of one to nine lowercase letters each followed by a space, and is stored below the typed layers, so that only the store is measured.
Each of the 100,000 keystrokes, 10,000 with `--quick`:

| share | keystroke |
| --- | --- |
| 85 % | type a letter at the cursor, which moves past it |
| 10 % | delete the byte before the cursor, if there is one |
| 4.5 % | move the cursor |
| 0.5 % | paste 200 random letters at the cursor, which moves past them |

Nine moves in ten land in a window of a tenth of the text, which jumps to a random place every 5000 keystrokes, and the tenth anywhere; `--focus` sets the window's share, `--focus-moves` the keystrokes between its jumps, and `--focus 0` sends every move anywhere.
The text grows with the typing, to about 3.7 times its size at the defaults.

**Every 1000 keystrokes the store is flushed and a row goes to `text-<variant>.csv`**, where the variant is the text's size in KiB, as in `64k`, unless `--variant` names it.
`app_bytes` counts the bytes typed and pasted; deletions count nothing.
`--journal-budget` sets `Options::journal_budget_pages`, so that the store also flushes by itself whenever a transaction would not fit in that many journal pages; `--shift-decay`, `--churn-floor` and `--target-fill` set the options of those names.

The summary on stderr has five lines:

1. the keystrokes of each kind, the text's size before and after, and the flushes, of them those at rows;
2. what kladde wrote, per keystroke, split into the journal, data pages, table pages and headers;
3. the bytes per keystroke that splices shifted, restated where they lie and written again;
4. the statements, fragments, table pages and data pages the file holds at the end, and the median flush;
5. the closed file's size, over the text's, and the median of 21 reopenings.

## `svg-bench`: a drawing editor

```sh
crates/kladde-svg/corpus/fetch.sh
cargo run --release -p kladde-svg --bin svg-bench [--features LAYOUT] -- \
    <drawing.svg> <output directory> [--quick] [--edits N] [--only KIND] [--seed N] [--variant NAME] \
    [--focus SHARE] [--focus-moves N] \
    [--journal-budget PAGES] [--shift-decay FACTOR] [--churn-floor λ] [--target-fill τ]
```

**kladde-svg stores an SVG drawing in a model of `#[derive(Persistable)]` types, and `svg-bench` edits it with a seeded mix of what a drawing editor does.**
`fetch.sh` downloads the corpus, pinned by SHA-256 and not committed: the Ghostscript tiger, the coat of arms of Spain and a blank world map from Wikimedia Commons, under `drawings/`, and resvg's test suite at v0.48.1, under `resvg/`.

| share | edit | what it does to the model |
| --- | --- | --- |
| 30 % | `Drag` | set the translation that leads a shape's transform list, or insert one |
| 20 % | `MoveNode` | set the end point of a segment of a path's data |
| 8 % / 7 % | `InsertNode` / `DeleteNode` | insert a relative line into a path's data, or delete a segment other than the first |
| 10 % | `Recolor` | set a shape's fill colour where it is set, in its attributes or its style, or add a fill attribute |
| 8 % / 7 % | `Duplicate` / `Delete` | insert a deep copy of a shape right above it, or delete one; each turns into the other when the drawing has grown or shrunk by a fifth |
| 5 % | `Restack` | move a shape elsewhere among its siblings, with `move_element` |
| 5 % | `Text` | type into a text, or replace it once it is long; a recolour in a drawing without text |

**The edits keep to one part of the drawing at a time**: nine in ten pick from a window of a tenth of what they pick from, the shapes, the paths, the shapes without children or the texts, and the window jumps every 3000 edits.
`--focus` and `--focus-moves` set the window as in `text-bench`; `--focus 0` picks every edit from the whole drawing, except that nine drags in ten move one of the first tenth of the shapes, which was the benchmark's only mix before kladde-rs `e88fd47`.
`--only KIND` makes every edit one kind.

**A run ends once the edits have stored eight times the drawing's live size, once with `--quick`, or after exactly `N` edits with `--edits`**, which is how runs of different layouts, branches or options make the same edits: the evaluation pages give each drawing's number.
Every 1000 edits the drawing is flushed and a row goes to `svg-<variant>.csv`, where the variant is the drawing's file name unless `--variant` names it.
`app_bytes` counts what each edit stores: the bytes of the numbers it sets, or the encoding of the value it stores, in its place, with everything that value owns; deletions count nothing.
The options of the store are set as in `text-bench`.

**The layouts are cargo features of kladde-svg, each adding to the one before**: none, the *slotted* layout of fixed slots; `packed`, lists of enums packed; `small-ids`, ids as small strings; `small-attrs`, attribute lists and strings small; `small`, every list small.
`f64` stores decimals as `f64` instead of `f32`.
Each layout is a build of its own, so a script that compares them builds once per feature and copies the binary aside.

The summary on stderr gives the drawing's size in SVG and in kladde and what it holds to edit; the edits, the bytes they stored and the mix as it came out; what kladde wrote, per edit and per byte stored, and the file over its live size; what saving the whole canonical SVG every 1, 100 or 1000 edits would have written instead; the edits per second in kladde's calls and the median flush; the closed file's size; and the times to parse the SVG, to reopen the closed file, as the median of 21, and to write the SVG.
After the edits, the reopened drawing must write the same canonical SVG as before closing.

## `svg-roundtrip`: a corpus through kladde

```sh
cargo run --release -p kladde-svg --bin svg-roundtrip [--features LAYOUT,render] -- \
    [--csv <table.csv>] [--render] [--journal-budget PAGES] <file or directory> ...
```

**For every `.svg` file under the paths, it checks that the canonical form is stable and that kladde gives it back byte for byte, and records the sizes of the file in each form.**
The canonical form is what kladde-svg writes: no whitespace between tags outside text, single spaces between attributes, and path data in the grammar's shortcuts.
It exits with a failure if any canonical form was not idempotent or any trip through kladde differed.

| column | meaning |
| --- | --- |
| `file`, `status` | the file, and `ok`, `not-idempotent`, `kladde-mismatch`, `not-xml`, `not-utf8` or `unreadable` |
| `orig_bytes`, `orig_gz`, `canon_bytes`, `canon_gz` | the original and the canonical form, plain and gzipped at the highest level |
| `kladde_bytes` | the kladde file after one flush and `close` |
| `settled_bytes` | the same file once two more sessions have opened and closed it, which returns the free pages a new file holds ahead of its data |
| `alloc_bytes`, `allocations`, `statements` | the store's live allocation bytes, allocations and statements |
| `data_pages`, `table_pages`, `free_pages` | the first-closed file's pages by kind |
| `numbers`, `numbers_changed` | the numbers stored, and those whose shortest decimal changed by being stored as the model's number type |
| `attrs`, `other_attrs`, `attr_bytes`, `other_attr_bytes` | attributes, and those the model kept verbatim, by count and bytes |
| `idempotent`, `kladde_ok` | the two checks, as 1 or 0 |
| `render_max_diff`, `render_diff_share` | with `--render`: the largest difference of any channel of any pixel between the original and the canonical form, rendered by resvg, and the share of pixels that differ by more than 1; a difference is a gap in the model, not a defect of kladde |
| `parse_us`, `open_us` | the time to parse the SVG, and to reopen the kladde file |

The summary on stderr counts the files by status, names those that failed, and sums the sizes.

`svg2kladde <in.svg> <out.kladde>` and `kladde2svg [--indent] <in.kladde> <out.svg>`, binaries of the same crate, convert single files, for looking at one by hand.

## Tools in this repository

### The figures

```sh
tools/plot-evaluation.py [--only NAMES] [--summary] --out <directory> <label>=<directory> ...
```

**Draws the evaluation pages' figures from tables, gzipped or not, each directory a run and each label its name in the figures.**
It recognizes the tables by name: `kladde-bench`'s by their scenario, `svg-bench`'s as `svg-<drawing>.csv`, and `text-bench`'s as `text-<size>.csv`.
`--only` draws the figures it names, out of `space-amplification`, `mixed`, `live-fraction`, `write-breakdown`, `budget`, `shrink`, `description`, `flush-latency`, `throughput`, `tradeoff`, `defrag-share`, `kappa`, `svg`, `svg-tradeoff`, `svg-pages` and `text-tradeoff`; a figure whose tables are missing is skipped.
`svg-tradeoff` and `text-tradeoff` also draw the runs labelled `before/policy/knob/value/layout`, dashed and pale, to show how a change moved each policy's curve.
`--summary` prints the last row of every series as a Markdown table.
The figures are SVG, the same bytes whenever the data and matplotlib's version are, which the inline metadata of `tools/plot-evaluation.py` pins; `uv run` installs it.

Each evaluation page keeps its tables, gzipped, in `content/evaluation/data/`, and lists the commands that ran them and that redraw its figures.

### The ripeness simulation

```sh
tools/simulate-ripeness.py compare|sweep|example [--set drafts|bayes] [options]
```

**Simulates pages whose statements die at the rates of the shares they belong to, cleans each page when an estimator says it is ripe, and prints how much more each estimator costs than cleaning at the true ripeness**, over all pages of a scenario.
`compare` runs every estimator of a set, the [ripeness drafts'](../drafts/ripeness.md) or with `--set bayes` the [Bayesian draft's](../drafts/bayesian-ripeness.md); `sweep` varies their parameters `β`, `n0` and `c`; `example` computes the draft's worked example without noise.
Its module docstring lists the estimators and the options.

### Checks of this repository

| command | checks |
| --- | --- |
| `tools/check-examples.py` | the Rust examples these pages mark with a `kladde-example` comment, compiled against a checkout of kladde-rs next to this one, and run, or refused by the compiler, where the comment says so |
| `tools/make-pdf.py --check-only` | the links and anchors between pages, and that `tools/pages.py` lists every page |
| `tools/make-pdf.py --self-test` | the layout passes of the diagrams |
| `site/build.sh`, then `tools/check-site-links.py` | the links and anchors of the built site, as Quartz made them |

[The README](https://github.com/kladde-dev/kladde-docs#tooling) describes them in full.

### Continuous integration

`.github/workflows/site.yml` runs on every push and pull request: it builds the PDF, builds the site and checks its links, and compiles and runs the examples against kladde-rs's `main`.
A broken link fails the run; a failing example does not hold up publishing.
On `main`, it then publishes the site and the PDF.

## A measurement kept with its evaluation

The heap an open drawing takes was measured once, for the evaluation of variable-size values on this repository's branch `defrag-shifts`, by `content/evaluation/data/variable-size/memory.rs` there.
It counts the bytes allocated through a global allocator while a drawing is parsed and while its kladde file is opened, and the median time of 21 opens, one drawing per call; it is built as the binary of a crate of its own that depends on kladde and kladde-svg, with their layout features.
