"""Render a static benchmark figure from saved measurements; requires matplotlib."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    report = json.loads(a.results.read_text())
    rows = report["results"]
    a.output.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "axes.spines.bottom": False,
        }
    )
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), layout="constrained")
    fig.set_facecolor("#f8fafc")
    labels = [f"{r['observations']:,}" for r in rows]
    for ax, key, title, unit, color in [
        (
            axes[0],
            "wall_seconds",
            "Complete import + assessment + writeback",
            "Seconds",
            "#6052d9",
        ),
        (
            axes[1],
            "peak_memory_mib",
            "Peak import/assessment container memory",
            "MiB",
            "#0d9488",
        ),
    ]:
        ax.set_facecolor("#f8fafc")
        values = [r[key] for r in rows]
        bars = ax.bar(labels, values, color=color, width=0.55)
        ax.bar_label(
            bars, labels=[f"{v:,.1f}" for v in values], padding=5, fontweight="bold"
        )
        ax.set_title(title, loc="left", fontsize=12, pad=14)
        ax.set_ylabel(unit)
        ax.set_xlabel("Observations + equally many source scores", labelpad=12)
        ax.set_ylim(0, max(values) * 1.25)
        ax.grid(axis="y", alpha=0.15)
        ax.set_axisbelow(True)
    fig.suptitle(
        "Witdem for Langfuse · bounded backfill benchmark",
        fontsize=19,
        fontweight="bold",
        x=0.02,
        ha="left",
    )
    fig.supxlabel(
        "Synthetic HTTP source + identity-verifying sink · real Linux container and Duckle\n2 CPU / 1 GiB limit · 2 matching evaluations per execution · one measured run per size",
        fontsize=10,
        color="#475569",
    )
    fig.savefig(a.output, dpi=180, facecolor=fig.get_facecolor())
    print(a.output)


if __name__ == "__main__":
    main()
