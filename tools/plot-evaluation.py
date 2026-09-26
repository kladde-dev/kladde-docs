#!/usr/bin/env python3
"""Turn kladde-bench's measurements into the figures of content/evaluation/.

Every argument after the options names a run, `label=directory`, where the
directory holds the CSV tables `kladde-bench` wrote (`uniform.csv`, ...),
plain or gzipped (`uniform.csv.gz`).
With one run, the figures show that run; with several, the figures that
compare policies overlay them, labelled.
`--only` draws only the figures it names, such as `--only tradeoff,kappa`.
The figures are SVG, written so that the same data gives the same bytes,
with every coordinate rounded to a hundredth of a point.

Usage:
    tools/plot-evaluation.py --out content/evaluation/figures/consolidation \\
        main=content/evaluation/data/consolidation

Needs matplotlib (`pip install matplotlib`, in a virtual environment if the
system Python is externally managed).
"""

import argparse
import csv
import gzip
import io
import re
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, NullFormatter  # noqa: E402

PAGE = 4096
CONTENT = 4081  # a page's content capacity
MIB = 1024 * 1024

plt.rcParams.update(
    {
        "svg.fonttype": "none",  # keep text as text: smaller, and searchable
        "svg.hashsalt": "kladde",  # the same element ids on every run
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "legend.frameon": False,
        "figure.dpi": 100,
    }
)

SIZE_COLORS = {1 * MIB: "#4c72b0", 8 * MIB: "#dd8452", 16 * MIB: "#8172b3", 64 * MIB: "#55a868"}
RUN_STYLES = ["-", "--", ":", "-."]


def num(v):
    try:
        return int(v)
    except ValueError:
        return float(v)


def load(directory):
    """{table: {(variant, size): [row, ...]}} with numeric fields, from the
    directory's `<table>.csv` or `<table>.csv.gz` files."""
    runs = {}
    for path in sorted(Path(directory).glob("*.csv*")):
        name, ext = path.name.split(".", 1)
        opener = {"csv": open, "csv.gz": gzip.open}.get(ext)
        if opener is None:
            continue
        series = defaultdict(list)
        with opener(path, "rt", newline="") as f:
            for row in csv.DictReader(f):
                r = {k: (v if k in ("scenario", "variant") else num(v)) for k, v in row.items()}
                series[(r["variant"], r["size"])].append(r)
        runs[name] = dict(series)
    return runs


def size_label(size):
    return f"{size // MIB} MiB" if size >= MIB else f"{size // 1024} KiB"


def written(r):
    """Bytes written to the file so far: pages, headers, and the journal."""
    return (r["data_written"] + r["table_written"] + r["headers_written"]) * PAGE + r["journal_bytes"]


def x_writes(rows, size):
    """Application bytes written, in multiples of the live size."""
    return [r["app_bytes"] / size for r in rows]


# Numbers in attribute values -- coordinates, in points -- with more than two
# decimals.  Text, such as a tick label, is content and is left alone.
ATTRIBUTE = re.compile(rb'(\s[\w:-]+=")([^"]*)(")')
LONG_DECIMAL = re.compile(rb"-?\d+\.\d{3,}")


def round_coordinates(svg):
    """Rounds every coordinate in `svg` to a hundredth of a point.

    Matplotlib writes six decimals, which is where most of a figure's bytes
    go, and a hundredth of a point is far below a pixel.
    """

    def number(m):
        s = (b"%.2f" % float(m.group())).rstrip(b"0").rstrip(b".")
        return b"0" if s == b"-0" else s

    def attribute(m):
        return m.group(1) + LONG_DECIMAL.sub(number, m.group(2)) + m.group(3)

    return ATTRIBUTE.sub(attribute, svg)


def save(fig, out, name):
    """Writes `<name>.svg`, byte for byte the same whenever the data is -- no
    date in it, and every coordinate rounded."""
    out.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    svg = io.BytesIO()
    fig.savefig(svg, format="svg", metadata={"Date": None})
    plt.close(fig)
    path = out / f"{name}.svg"
    path.write_bytes(round_coordinates(svg.getvalue()))
    print(f"wrote {path}")


def plain_log(ax):
    """A logarithmic y axis labelled with plain numbers."""
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def rolling_median(values, window=21):
    """Each value replaced by the median of the `window` around it."""
    half = window // 2
    return [statistics.median(values[max(0, i - half):i + half + 1]) for i in range(len(values))]


def legend(ax, **kwargs):
    """A legend, if anything is labelled."""
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=7, **kwargs)


def space_amplification(runs, out):
    """File size over the live allocation size, as the writes accumulate."""
    scenarios = [s for s in ("uniform", "skewed") if any(s in r for r in runs.values())]
    fig, axes = plt.subplots(1, len(scenarios), figsize=(7.5, 3.2), sharey=True, squeeze=False)
    for ax, scenario in zip(axes[0], scenarios):
        for style, (label, run) in zip(RUN_STYLES, runs.items()):
            for (variant, size), rows in sorted(run.get(scenario, {}).items(), key=lambda kv: (kv[0][0] != "on", kv[0][1])):
                if variant == "off" and len(runs) > 1:
                    continue
                y = [r["file_pages"] * PAGE / max(r["alloc_bytes"], 1) for r in rows]
                name = f"{size_label(size)}" + ("" if variant == "on" else ", no consolidation")
                if len(runs) > 1:
                    name = f"{label}, {name}"
                ax.plot(x_writes(rows, size), y, style if variant == "on" else ":", color=SIZE_COLORS.get(size), label=name, lw=1.2)
        ax.set_title(f"{scenario} overwrites")
        ax.set_xlabel("bytes written / live size")
        ax.set_ylim(1, None)
    axes[0][0].set_ylabel("file size / live size")
    legend(axes[0][-1])
    save(fig, out, "space-amplification")


def mixed(runs, out):
    """The `mixed` workload, where hot, cool, and cold allocations share pages
    at random, as the writes accumulate: file size over live size, the live
    fraction of the data pages, the share of the live data on pages that hold
    more than one class, and the share of the cold data on such pages. The
    1 MiB files are left out, and a run without consolidation is drawn once,
    since policies do not change it."""
    series = [(label, run["mixed"]) for label, run in runs.items() if "mixed" in run]
    if not series:
        return
    fig, grid = plt.subplots(2, 2, figsize=(7.5, 5.4), sharex=True)
    axes = grid.flatten()
    for n, (style, (label, table)) in enumerate(zip(RUN_STYLES, series)):
        for (variant, size), rows in sorted(table.items(), key=lambda kv: (kv[0][0] != "on", kv[0][1])):
            if size < 8 * MIB or (variant == "off" and n > 0):
                continue
            name = size_label(size) + ("" if variant == "on" else ", no consolidation")
            if len(series) > 1 and variant == "on":
                name = f"{label}, {name}"
            x = x_writes(rows, size)
            ls, color = (style, SIZE_COLORS.get(size)) if variant == "on" else (":", "#8c8c8c")
            live = [max(r["alloc_bytes"], 1) for r in rows]
            axes[0].plot(x, [r["file_pages"] * PAGE / l for r, l in zip(rows, live)], ls, color=color, label=name, lw=1.2)
            axes[1].plot(x, [r["live_data"] / max(r["data_pages"] * CONTENT, 1) for r in rows], ls, color=color, lw=1.2)
            axes[2].plot(x, [r["mixed_bytes"] / l for r, l in zip(rows, live)], ls, color=color, lw=1.2)
            axes[3].plot(x, [r["cold_mixed_bytes"] / max(r["cold_bytes"], 1) for r in rows], ls, color=color, lw=1.2)
    axes[0].set_ylabel("file size / live size")
    axes[0].set_ylim(1, None)
    axes[1].set_ylabel("live fraction of data pages")
    axes[2].set_ylabel("share of the live data\non pages that mix classes")
    axes[3].set_ylabel("share of the cold data\non pages that mix classes")
    for ax in axes[1:]:
        ax.set_ylim(0, 1)
    for ax in axes[2:]:
        ax.set_xlabel("bytes written / live size")
    legend(axes[0])
    save(fig, out, "mixed")


def live_fraction(runs, out, size=64 * MIB):
    """How full the data pages and the address-table pages are."""
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.0), sharey=True)
    for ax, kind in zip(axes, ("data", "table")):
        for style, (label, run) in zip(RUN_STYLES, runs.items()):
            for scenario, color in (("uniform", "#4c72b0"), ("skewed", "#c44e52")):
                rows = run.get(scenario, {}).get(("on", size))
                if not rows:
                    continue
                pages = [r[f"{kind}_pages"] for r in rows]
                live = [r[f"live_{kind}"] for r in rows]
                y = [l / (p * CONTENT) if p else 1 for l, p in zip(live, pages)]
                name = scenario if len(runs) == 1 else f"{label}, {scenario}"
                ax.plot(x_writes(rows, size), y, style, color=color, label=name, lw=1.2)
        ax.set_title(f"{kind} pages, {size_label(size)}")
        ax.set_xlabel("bytes written / live size")
        ax.set_ylim(0, 1.02)
    axes[0].set_ylabel("live fraction")
    legend(axes[1])
    save(fig, out, "live-fraction")


def write_breakdown(runs, out):
    """Where the bytes written to the file went, per byte the application
    wrote, over each whole scenario, runs side by side. The rest of the data
    pages -- neither the application's new content nor content consolidation
    moved -- is the room pages closed with, and the consolidator state."""
    labels, parts = [], defaultdict(list)
    for scenario in ("uniform", "skewed", "append", "churn", "typed"):
        sizes = sorted({size for run in runs.values() for (_, size) in run.get(scenario, {})})
        for size in sizes:
            if scenario in ("uniform", "skewed") and size not in (8 * MIB, 64 * MIB):
                continue
            for label, run in runs.items():
                rows = run.get(scenario, {}).get(("on", size))
                if not rows:
                    continue
                r = rows[-1]
                app = max(r["app_bytes"], 1)
                consolidation = r["evacuated_bytes"] + r["defrag_bytes"]
                data = r["data_written"] * PAGE
                fresh = min(r["fresh_bytes"], data)
                parts["journal"].append(r["journal_bytes"] / app)
                parts["data pages: new content"].append(fresh / app)
                parts["data pages: consolidation"].append(min(consolidation, data - fresh) / app)
                parts["data pages: other"].append(max(data - fresh - consolidation, 0) / app)
                parts["address-table pages"].append(r["table_written"] * PAGE / app)
                parts["headers"].append(r["headers_written"] * PAGE / app)
                name = f"{scenario}\n{size_label(size)}"
                labels.append(name if len(runs) == 1 else f"{name}\n{label}")
    if not labels:
        return
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    bottom = [0.0] * len(labels)
    colors = ["#8c8c8c", "#4c72b0", "#dd8452", "#dfc27d", "#55a868", "#c44e52"]
    for (part, values), color in zip(parts.items(), colors):
        ax.bar(range(len(labels)), values, bottom=bottom, label=part, color=color, width=0.7)
        bottom = [b + v for b, v in zip(bottom, values)]
    ax.set_xticks(range(len(labels)), labels, fontsize=7)
    ax.set_ylabel("bytes written to the file\nper byte the application wrote")
    ax.legend(fontsize=7, ncols=3, loc="lower center", bbox_to_anchor=(0.5, 1.0))
    save(fig, out, "write-breakdown")


def budget(runs, out):
    """The budget the controller sets: with the default target fill, and with
    targets the churn floor lets the file reach."""
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.0), sharey=True)
    for style, (label, run) in zip(RUN_STYLES, runs.items()):
        prefix = "" if len(runs) == 1 else f"{label}, "
        for scenario in ("uniform", "skewed"):
            ls = style if scenario == "uniform" else "--"
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if variant == "on":
                    axes[0].plot(x_writes(rows, size), [r["budget"] for r in rows], ls, color=SIZE_COLORS.get(size),
                                 label=f"{prefix}{scenario}, {size_label(size)}", lw=1)
            tuning = run.get(f"tuning-{scenario}", {})
            for (variant, size), rows in sorted(tuning.items()):
                if variant.startswith("tau"):
                    color = "#dd8452" if variant == "tau=0.6" else "#c44e52"
                    axes[1].plot(x_writes(rows, size), [r["budget"] for r in rows], ls, color=color,
                                 label=f"{prefix}{scenario}, {pretty(variant)}", lw=1)
    axes[0].set_title("target fill τ = 0.8 (the default)")
    axes[1].set_title("lower targets, 8 MiB")
    axes[0].set_ylabel("budget (pages per flush)")
    for ax in axes:
        ax.set_xlabel("bytes written / live size")
        plain_log(ax)
        legend(ax, ncols=2 if ax is axes[0] else 1, loc="lower right", frameon=True, framealpha=0.9, edgecolor="none")
    save(fig, out, "budget")


def shrink(runs, out, flushes=60):
    """File size after three quarters of it were freed, over the first
    `flushes` flushes; it changes little after that."""
    fig, ax = plt.subplots(figsize=(7.5, 2.8))
    looks = {
        "on": ("compaction mode", "#55a868", 0),
        "no-compaction": ("consolidation without compaction mode", "#4c72b0", 1),
        "off": ("no consolidation", "#dd8452", 2),
    }
    for style, (label, run) in zip(RUN_STYLES, runs.items()):
        for (variant, size), rows in sorted(run.get("shrink", {}).items()):
            name, color, dash = looks.get(variant, (variant, None, 0))
            live = rows[-1]["alloc_bytes"]
            rows = [r for r in rows if r["flush"] <= flushes]
            y = [r["file_pages"] * PAGE / live for r in rows]
            ls = [style, (0, (5, 3)), (0, (1, 2))][dash] if len(runs) == 1 else style
            ax.plot([r["flush"] for r in rows], y, linestyle=ls, color=color,
                    label=name if len(runs) == 1 else f"{label}, {name}", lw=1.4)
    ax.set_xlabel("flushes after the frees")
    ax.set_ylabel("file size / live size")
    ax.set_ylim(0, None)
    legend(ax)
    save(fig, out, "shrink")


def description(runs, out):
    """How many statements describe the data, and how full the file is, in the
    scenarios where small writes accumulate."""
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.0))
    for style, (label, run) in zip(RUN_STYLES, runs.items()):
        for scenario, color in (("append", "#4c72b0"), ("churn", "#dd8452")):
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if len(runs) > 1 and variant != "on":
                    continue
                ls = style if variant == "on" else ":"
                name = scenario + ("" if variant == "on" else ", no consolidation")
                if len(runs) > 1:
                    name = f"{label}, {name}"
                x = x_writes(rows, size)
                axes[0].plot(x, [r["statements"] / (r["alloc_bytes"] / 1024) for r in rows], ls, color=color, label=name, lw=1.2)
                axes[1].plot(x, [r["file_pages"] * PAGE / max(r["alloc_bytes"], 1) for r in rows], ls, color=color, label=name, lw=1.2)
    axes[0].set_ylabel("statements per KiB of live data")
    axes[1].set_ylabel("file size / live size")
    axes[1].set_ylim(0, None)
    for ax in axes:
        ax.set_xlabel("bytes written / live size")
    legend(axes[1])
    save(fig, out, "description")


def flush_latency(runs, out):
    """How long a flush takes, including its fsync, per scenario: the median
    and the 90th and 99th percentiles of one run, or, of several, each run's
    median as a bar and its 99th percentile as a tick."""
    order, times = [], defaultdict(dict)
    for label, run in runs.items():
        for scenario in ("uniform", "skewed", "append", "churn", "typed"):
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                t = sorted(r["flush_us"] / 1000 for r in rows)
                if variant != "on" or not t:
                    continue
                if (scenario, size) not in times:
                    order.append((scenario, size))
                times[(scenario, size)][label] = (statistics.median(t), t[int(0.9 * (len(t) - 1))], t[int(0.99 * (len(t) - 1))])
    labels = [f"{scenario}\n{size_label(size)}" for scenario, size in order]
    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    xs = range(len(labels))
    if len(runs) == 1:
        (label,) = runs
        for i, (name, color) in enumerate((("median", "#4c72b0"), ("90th percentile", "#dd8452"), ("99th percentile", "#c44e52"))):
            ax.bar([x + (i - 1) * 0.25 for x in xs], [times[k][label][i] for k in order], width=0.25, label=name, color=color)
    else:
        width = 0.8 / len(runs)
        for i, (label, color) in enumerate(zip(runs, ["#4c72b0", "#c44e52", "#55a868", "#8172b3"])):
            at = [x + (i - (len(runs) - 1) / 2) * width for x in xs]
            ax.bar(at, [times[k].get(label, (float("nan"),) * 3)[0] for k in order], width=width * 0.9, color=color,
                   label=f"{label}: median")
            ax.scatter(at, [times[k].get(label, (float("nan"),) * 3)[2] for k in order], marker="_", s=80, color=color,
                       label=f"{label}: 99th percentile", zorder=3)
    ax.set_xticks(list(xs), labels, fontsize=7)
    ax.set_ylabel("flush time (ms)")
    legend(ax, ncols=1 if len(runs) == 1 else 2)
    save(fig, out, "flush-latency")


def throughput(runs, out):
    """Operations per second, flushes included, as the scenarios run: the
    median of every 21 flushes around each, leaving out a last flush of
    fewer operations."""
    fig, ax = plt.subplots(figsize=(7.5, 2.8))
    for style, (label, run) in zip(RUN_STYLES, runs.items()):
        for scenario, color in (("uniform", "#4c72b0"), ("skewed", "#c44e52"), ("append", "#55a868"), ("churn", "#dd8452"), ("typed", "#8172b3")):
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if variant != "on" or (scenario in ("uniform", "skewed") and size != 64 * MIB):
                    continue
                x, y, prev = [], [], 0
                for r, xr in zip(rows, x_writes(rows, size)):
                    ops = r["ops"] - prev
                    prev = r["ops"]
                    if ops < 500:
                        continue
                    x.append(xr)
                    y.append(ops / ((r["flush_us"] + r["ops_us"]) / 1e6))
                name = f"{scenario}, {size_label(size)}"
                if len(runs) > 1:
                    name = f"{label}, {name}"
                ax.plot(x, [v / 1000 for v in rolling_median(y)], style, color=color, label=name, lw=1)
    ax.set_xlabel("bytes written / live size")
    ax.set_ylabel("thousand operations per second")
    ax.set_ylim(0, None)
    legend(ax, ncols=3, loc="upper center", bbox_to_anchor=(0.5, 1.18))
    save(fig, out, "throughput")


SYMBOLS = {"lambda": "λ", "tau": "τ", "share": "defrag share"}
KNOBS = {"lambda": "churn floor λ", "tau": "target fill τ", "share": "defragmentation share"}


def pretty(variant):
    """`lambda=0.5` as `λ = 0.5`."""
    name, _, value = variant.partition("=")
    name = SYMBOLS.get(name, name)
    return f"{name} = {value}" if value else name


def steady(rows):
    """The steady state of a run: its file size over live size, averaged
    over the second half of its flushes, and the bytes it wrote per byte the
    application wrote during that half."""
    mid, end = rows[len(rows) // 2], rows[-1]
    space = statistics.mean(r["file_pages"] * PAGE / max(r["alloc_bytes"], 1) for r in rows[len(rows) // 2:])
    writes = (written(end) - written(mid)) / max(end["app_bytes"] - mid["app_bytes"], 1)
    return writes, space


def tradeoff(runs, out):
    """Space against writes in the steady state of the overwrite workloads'
    variants: a curve through the variants of the knob a run varies most
    (the churn floor `λ`, or else the target fill `τ`), and the other
    variants as points, labelled."""
    scenarios = [s for s in ("uniform", "skewed") if any(f"tuning-{s}" in r for r in runs.values())]
    if not scenarios:
        return
    fig, axes = plt.subplots(1, len(scenarios), figsize=(7.5, 3.4), sharey=True, squeeze=False)
    colors = ["#4c72b0", "#c44e52", "#55a868", "#8172b3"]
    for ax, scenario in zip(axes[0], scenarios):
        size = None
        # A variant is labelled at the first run that has it: a later run's
        # curve through the same variants runs in the same order.
        labelled = set()
        for n, (color, (label, run)) in enumerate(zip(colors, runs.items())):
            series = run.get(f"tuning-{scenario}", {})
            points = {}
            for (variant, size), rows in series.items():
                points[variant] = steady(rows)
            if not points:
                continue
            knobs = defaultdict(list)
            for v in points:
                knobs[v.split("=")[0]].append(v)
            curve = max(knobs, key=lambda k: len(knobs[k]))
            ordered = sorted(knobs[curve], key=lambda v: float(v.split("=")[1]))
            prefix = "" if len(runs) == 1 else f"{label}: "
            ax.plot([points[v][0] for v in ordered], [points[v][1] for v in ordered], "-o", color=color, ms=4, lw=1,
                    label=f"{prefix}{KNOBS.get(curve, curve)}")
            rest = [v for v in points if v not in ordered]
            if rest:
                other = rest[0].split("=")[0]
                ax.plot([points[v][0] for v in rest], [points[v][1] for v in rest], "s", color=color, ms=4, mfc="white",
                        label=f"{prefix}{KNOBS.get(other, other)}")
            # The first run labels above its points, the next below, so that
            # labels of nearby points of two runs stay apart.
            offset = (4, 4) if n % 2 == 0 else (4, -10)
            for v, (x, y) in points.items():
                if v not in labelled:
                    ax.annotate(pretty(v), (x, y), textcoords="offset points", xytext=offset, fontsize=6.5, color=color)
            labelled.update(points)
        ax.set_title(f"{scenario} overwrites" + (f", {size_label(size)}" if size else ""))
        ax.set_xlabel("bytes written to the file / by the application")
        ax.set_ylim(1, None)
    axes[0][0].set_ylabel("file size / live size")
    legend(axes[0][-1])
    save(fig, out, "tradeoff")


def kappa(runs, out):
    """The price of space the controller sets, where a run records it."""
    series = [(label, run) for label, run in runs.items()
              if any("kappa" in rows[0] for table in run.values() for rows in table.values())]
    if not series:
        return
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.0), sharey=True)
    for style, (label, run) in zip(RUN_STYLES, series):
        for scenario in ("uniform", "skewed"):
            ls = style if scenario == "uniform" else "--"
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if variant == "on":
                    axes[0].plot(x_writes(rows, size), [r["kappa"] for r in rows], ls, color=SIZE_COLORS.get(size),
                                 label=f"{scenario}, {size_label(size)}", lw=1)
            tuning = run.get(f"tuning-{scenario}", {})
            shades = plt.cm.viridis([i / max(len(tuning) - 1, 1) for i in range(len(tuning))])
            ordered = sorted(tuning.items(), key=lambda kv: float(kv[0][0].split("=")[1]))
            for shade, ((variant, size), rows) in zip(shades, ordered):
                if scenario == "uniform":
                    axes[1].plot(x_writes(rows, size), [r["kappa"] for r in rows], ls, color=shade,
                                 label=pretty(variant), lw=1)
    axes[0].set_title("default target fill")
    axes[1].set_title("uniform overwrites, 8 MiB, by target fill")
    axes[0].set_ylabel("price of space κ")
    for ax in axes:
        ax.set_xlabel("bytes written / live size")
        plain_log(ax)
        legend(ax, ncols=2 if ax is axes[0] else 1, loc="lower right", frameon=True, framealpha=0.9, edgecolor="none")
    save(fig, out, "kappa")


def defrag_share(runs, out):
    """Statements per KiB of live data in the append workload, for three
    reserved shares of description defragmentation."""
    series = [(label, run["tuning-append"]) for label, run in runs.items() if "tuning-append" in run]
    if not series:
        return
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.0))
    colors = ["#4c72b0", "#dd8452", "#55a868", "#c44e52"]
    for style, (label, table) in zip(RUN_STYLES, series):
        ordered = sorted(table.items(), key=lambda kv: float(kv[0][0].split("=")[1]))
        for color, ((variant, size), rows) in zip(colors, ordered):
            name = pretty(variant) + ("" if len(series) == 1 else f", {label}")
            x = x_writes(rows, size)
            axes[0].plot(x, [r["statements"] / (r["alloc_bytes"] / 1024) for r in rows], style, color=color, label=name, lw=1.2)
            axes[1].plot(x, [written(r) / max(r["app_bytes"], 1) for r in rows], style, color=color, label=name, lw=1.2)
    axes[0].set_ylabel("statements per KiB of live data")
    axes[1].set_ylabel("bytes written to the file\nper byte the application wrote")
    axes[1].set_ylim(0, None)
    for ax in axes:
        ax.set_xlabel("bytes written / live size")
    legend(axes[0])
    save(fig, out, "defrag-share")


def summary(runs):
    """The end state of every series, as a Markdown table on stdout."""
    print("| run | scenario | variant | size | file/live | data fill | table fill | written/app | flushes | median flush ms |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, run in runs.items():
        for scenario, series in run.items():
            for (variant, size), rows in sorted(series.items()):
                r = rows[-1]
                data_fill = r["live_data"] / (r["data_pages"] * CONTENT) if r["data_pages"] else 1
                table_fill = r["live_table"] / (r["table_pages"] * CONTENT) if r["table_pages"] else 1
                amp = written(r) / max(r["app_bytes"], 1)
                med = statistics.median(x["flush_us"] for x in rows) / 1000
                print(
                    f"| {label} | {scenario} | {variant} | {size_label(size)} "
                    f"| {r['file_pages'] * PAGE / max(r['alloc_bytes'], 1):.2f} | {data_fill:.2f} "
                    f"| {table_fill:.2f} | {amp:.2f} | {len(rows)} | {med:.2f} |"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path, help="where the figures go, as SVG")
    parser.add_argument("--summary", action="store_true", help="also print the end states as a Markdown table")
    parser.add_argument("--only", help="draw only these figures, comma-separated names such as tradeoff,kappa")
    parser.add_argument("runs", nargs="+", help="label=directory of CSV tables")
    args = parser.parse_args()
    runs = {}
    for spec in args.runs:
        label, _, directory = spec.partition("=")
        runs[label] = load(directory)
    figures = [space_amplification, mixed, live_fraction, write_breakdown, budget, shrink, description, flush_latency,
               throughput, tradeoff, defrag_share, kappa]
    only = set(args.only.split(",")) if args.only else None
    for figure in figures:
        if only is None or figure.__name__.replace("_", "-") in only:
            figure(runs, args.out)
    if args.summary:
        summary(runs)


if __name__ == "__main__":
    main()
