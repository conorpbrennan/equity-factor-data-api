"""Cross-environment comparison report: N labeled summary.json grids side by
side, with ratios against the first (baseline) label."""

from __future__ import annotations

import json
from pathlib import Path

from . import ARMS, QUERIES


def _fmt_s(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def _fmt_ratio(v: float | None, base: float | None) -> str:
    if v is None or not base:
        return "—"
    r = v / base
    return f"{r:.1f}×" if r >= 9.95 else f"{r:.2f}×"


def compare(grids: list[tuple[str, Path]], out: Path | None) -> str:
    data = {label: json.loads(path.read_text())["cells"]
            for label, path in grids}
    labels = [label for label, _ in grids]
    base = labels[0]

    def cell(label: str, arm: str, query: str, mode: str) -> dict | None:
        return data[label].get(f"{arm}/{query}/{mode}")

    lines = [
        "# Benchmark comparison — " + " vs ".join(labels), "",
        f"p50 seconds per (query, arm); ratios are vs `{base}`. "
        "Bytes are median logical read volume per query execution.", "",
    ]
    for mode, title in (("warm", "Warm p50 (seconds)"),
                        ("cold", "Cold p50 (seconds)")):
        header = (["query", "arm"] + labels
                  + [f"{lb}/{base}" for lb in labels[1:]])
        lines += [f"## {title}", "",
                  "| " + " | ".join(header) + " |",
                  "|---" * len(header) + "|"]
        for query in QUERIES:
            for arm in ARMS:
                cells = {lb: cell(lb, arm, query, mode) for lb in labels}
                if not any(cells.values()):
                    continue
                b = cells[base]["p50_s"] if cells[base] else None
                row = ([query, f"`{arm}`"]
                       + [_fmt_s(c["p50_s"] if c else None) for c in cells.values()]
                       + [_fmt_ratio(cells[lb]["p50_s"] if cells[lb] else None, b)
                          for lb in labels[1:]])
                lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    header = ["query", "arm"] + [f"{lb} MB" for lb in labels]
    lines += ["## Bytes read (warm, median MB)", "",
              "| " + " | ".join(header) + " |",
              "|---" * len(header) + "|"]
    for query in QUERIES:
        for arm in ARMS:
            cells = {lb: cell(lb, arm, query, "warm") for lb in labels}
            if not any(cells.values()):
                continue
            row = ([query, f"`{arm}`"]
                   + ["—" if c is None else f"{c['mb_read']:,.0f}"
                      for c in cells.values()])
            lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    text = "\n".join(lines)
    if out:
        out.write_text(text)
        print(f"comparison -> {out}")
    return text
