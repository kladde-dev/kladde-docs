#!/usr/bin/env python3
"""Turn kladde-bench's measurements into the figures of content/evaluation/.

Every argument after the options names a run, `label=directory`, where the
directory holds the CSV tables `kladde-bench` wrote (`uniform.csv`, ...),
plain or gzipped (`uniform.csv.gz`).
With one run, the figures show that run; with several, the figures that
compare policies overlay them, labelled.
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
    wrote, at the end of each scenario."""
    labels, parts = [], defaultdict(list)
    for label, run in runs.items():
        for scenario in ("uniform", "skewed", "append", "churn", "typed"):
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if variant != "on" or (scenario in ("uniform", "skewed") and size != 64 * MIB and size != 8 * MIB):
                    continue
                r = rows[-1]
                app = max(r["app_bytes"], 1)
                consolidation = r["evacuated_bytes"] + r["defrag_bytes"]
                data = r["data_written"] * PAGE
                fresh = min(r["fresh_bytes"], data)
                parts["journal"].append(r["journal_bytes"] / app)
                parts["data pages: new content"].append(fresh / app)
                parts["data pages: consolidation"].append(min(consolidation, data - fresh) / app)
                parts["data pages: slack"].append(max(data - fresh - consolidation, 0) / app)
                parts["address-table pages"].append(r["table_written"] * PAGE / app)
                parts["headers"].append(r["headers_written"] * PAGE / app)
                name = f"{scenario}\n{size_label(size)}"
                labels.append(name if len(runs) == 1 else f"{label}\n{name}")
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
    """How long a flush takes, including its fsync, per scenario."""
    labels, med, p90, p99 = [], [], [], []
    for label, run in runs.items():
        for scenario in ("uniform", "skewed", "append", "churn", "typed"):
            for (variant, size), rows in sorted(run.get(scenario, {}).items()):
                if variant != "on":
                    continue
                t = sorted(r["flush_us"] / 1000 for r in rows)
                if not t:
                    continue
                name = f"{scenario}\n{size_label(size)}"
                labels.append(name if len(runs) == 1 else f"{label}\n{name}")
                med.append(statistics.median(t))
                p90.append(t[int(0.9 * (len(t) - 1))])
                p99.append(t[int(0.99 * (len(t) - 1))])
    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    xs = range(len(labels))
    ax.bar([x - 0.25 for x in xs], med, width=0.25, label="median", color="#4c72b0")
    ax.bar(xs, p90, width=0.25, label="90th percentile", color="#dd8452")
    ax.bar([x + 0.25 for x in xs], p99, width=0.25, label="99th percentile", color="#c44e52")
    ax.set_xticks(list(xs), labels, fontsize=7)
    ax.set_ylabel("flush time (ms)")
    legend(ax)
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


def pretty(variant):
    """`lambda=0.5` as `λ = 0.5`."""
    name, _, value = variant.partition("=")
    name = {"lambda": "λ", "tau": "τ", "share": "defrag share"}.get(name, name)
    return f"{name} = {value}" if value else name


def tradeoff(runs, out):
    """Space against writes, for variants of the churn floor and the target
    fill, at the end of the overwrite workloads."""
    tables = [(s, run[f"tuning-{s}"]) for run in runs.values() for s in ("uniform", "skewed") if f"tuning-{s}" in run]
    if not tables:
        return
    fig, axes = plt.subplots(1, len(tables), figsize=(7.5, 3.2), sharey=True, squeeze=False)
    for ax, (scenario, series) in zip(axes[0], tables):
        points = {}
        for (variant, size), rows in series.items():
            r = rows[-1]
            points[variant] = (written(r) / max(r["app_bytes"], 1), r["file_pages"] * PAGE / max(r["alloc_bytes"], 1))
        lam = sorted((v for v in points if v.startswith("lambda")), key=lambda v: float(v.split("=")[1]))
        ax.plot([points[v][0] for v in lam], [points[v][1] for v in lam], "-o", color="#4c72b0", ms=4, lw=1, label="churn floor λ")
        tau = [v for v in points if v.startswith("tau")]
        ax.plot([points[v][0] for v in tau], [points[v][1] for v in tau], "s", color="#dd8452", ms=4, label="target fill τ, with λ = 1")
        for v, (x, y) in points.items():
            ax.annotate(pretty(v), (x, y), textcoords="offset points", xytext=(5, 4), fontsize=7)
        ax.set_title(f"{scenario} overwrites, {size_label(size)}")
        ax.set_xlabel("bytes written to the file / by the application")
        ax.set_ylim(1, None)
    axes[0][0].set_ylabel("file size / live size")
    legend(axes[0][-1])
    save(fig, out, "tradeoff")


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
    parser.add_argument("runs", nargs="+", help="label=directory of CSV tables")
    args = parser.parse_args()
    runs = {}
    for spec in args.runs:
        label, _, directory = spec.partition("=")
        runs[label] = load(directory)
    space_amplification(runs, args.out)
    live_fraction(runs, args.out)
    write_breakdown(runs, args.out)
    budget(runs, args.out)
    shrink(runs, args.out)
    description(runs, args.out)
    flush_latency(runs, args.out)
    throughput(runs, args.out)
    tradeoff(runs, args.out)
    defrag_share(runs, args.out)
    if args.summary:
        summary(runs)


if __name__ == "__main__":
    main()
