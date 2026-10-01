"""Histogram rendering and REPORT.md/.html/.pdf assembly.

All of it -- histograms, REPORT.md, REPORT.html, REPORT.pdf -- is pure build
output of running ``benchmark.py``; nothing under ``reports/`` is hand-edited.
The Markdown is the single source of content: the HTML and PDF are both
rendered from the exact same Markdown string, just through different CSS, so
there is no separate copy to keep in sync.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import markdown as _markdown
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from harness import Samples  # noqa: E402
from xhtml2pdf import pisa  # noqa: E402


def _log_bins(values_ms: list[float], n: int = 30) -> list[float]:
    """Log-spaced bin edges spanning the observed range, for a log-scale x-axis histogram."""
    import numpy as np

    lo, hi = min(values_ms), max(values_ms)
    return list(np.logspace(np.log10(lo * 0.9), np.log10(hi * 1.1), n))


def plot_histograms(samples: list[Samples], out_dir: Path) -> dict[tuple[str, str, int], Path]:
    """Render one PNG per (interface, operation, scale), overlaying every backend's distribution."""
    out_dir.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, str, int], list[Samples]] = defaultdict(list)
    for sample in samples:
        groups[(sample.interface, sample.operation, sample.scale)].append(sample)

    paths: dict[tuple[str, str, int], Path] = {}
    for key in sorted(groups):
        interface, operation, scale = key
        group = groups[key]
        fig, ax = plt.subplots(figsize=(7, 4.5))

        all_ms = [v * 1000 for sample in group for v in sample.latencies_s if v > 0]
        # Backends in the same cell can differ by 2-3 orders of magnitude (a local in-memory
        # or index lookup next to a moto-server round trip); a linear-binned histogram would
        # flatten the faster backends into an invisible sliver at the left edge, so switch to
        # log-spaced bins whenever the spread actually warrants it.
        use_log = bool(all_ms) and (max(all_ms) / min(all_ms)) > 20
        bins = _log_bins(all_ms) if use_log else 30

        for sample in group:
            latencies_ms = [v * 1000 for v in sample.latencies_s]
            if not latencies_ms:
                continue
            ax.hist(latencies_ms, bins=bins, alpha=0.55, label=f"{sample.backend} (n={len(latencies_ms)})")
        if use_log:
            ax.set_xscale("log")
        ax.set_xlabel("latency (ms)" + (" [log scale]" if use_log else ""))
        ax.set_ylabel("count")
        ax.set_title(f"{interface}.{operation} @ scale={scale}")
        ax.legend(fontsize=8)
        fig.tight_layout()
        safe_operation = operation.replace(" ", "_")
        filename = f"{interface.lower()}_{safe_operation}_{scale}.png"
        path = out_dir / filename
        fig.savefig(path, dpi=120)
        plt.close(fig)
        paths[key] = path

    return paths


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def _compact_cell(stats: dict[str, float]) -> str:
    if stats["n"] == 0:
        return "n/a"
    return f"mean {_fmt(stats['mean_ms'])}ms<br>p95 {_fmt(stats['p95_ms'])}ms<br>{stats['throughput_ops_s']:.0f} ops/s"


def _compact_tables(samples: list[Samples]) -> str:
    by_interface_scale: dict[tuple[str, int], list[Samples]] = defaultdict(list)
    for sample in samples:
        by_interface_scale[(sample.interface, sample.scale)].append(sample)

    lines: list[str] = []
    for interface, scale in sorted(by_interface_scale, key=lambda k: (k[0], k[1])):
        group = by_interface_scale[(interface, scale)]
        backends = sorted({s.backend for s in group})
        operations = sorted({s.operation for s in group}, key=lambda op: (op != "write" and op != "add", op))
        lines.append(f"### {interface} @ scale={scale}\n")
        lines.append("| backend | " + " | ".join(operations) + " |")
        lines.append("|---|" + "---|" * len(operations))
        cell_by_key = {(s.backend, s.operation): s for s in group}
        for backend in backends:
            row = [backend]
            for operation in operations:
                sample = cell_by_key.get((backend, operation))
                row.append(_compact_cell(sample.stats) if sample is not None else "--")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines)


def _appendix_table(samples: list[Samples]) -> str:
    lines = [
        "| interface | operation | backend | scale | mean (ms) | median (ms) | p95 (ms) "
        "| p99 (ms) | throughput (ops/s) | n |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sample in sorted(samples, key=lambda s: (s.interface, s.operation, s.scale, s.backend)):
        stats = sample.stats
        lines.append(
            f"| {sample.interface} | {sample.operation} | {sample.backend} | {sample.scale} "
            f"| {_fmt(stats['mean_ms'])} | {_fmt(stats['median_ms'])} | {_fmt(stats['p95_ms'])} "
            f"| {_fmt(stats['p99_ms'])} | {stats['throughput_ops_s']:.1f} | {int(stats['n'])} |"
        )
    return "\n".join(lines)


_HTML_STYLE = """
<style>
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 1000px;
       margin: 2rem auto; padding: 0 1rem; line-height: 1.55; color: #1a1a1a; background: #fff; }
h1 { border-bottom: 3px solid #2c5aa0; padding-bottom: 0.3rem; }
h2 { border-bottom: 1px solid #ccc; padding-bottom: 0.2rem; margin-top: 2.5rem; color: #2c5aa0; }
h3 { margin-top: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.85rem; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
th { background: #2c5aa0; color: #fff; }
tr:nth-child(even) { background: #f4f7fb; }
img { max-width: 100%; height: auto; border: 1px solid #ddd; margin: 0.5rem 0; }
code { background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; font-family: Consolas, monospace; }
pre { background: #f0f0f0; padding: 0.8rem; overflow-x: auto; }
.table-scroll { overflow-x: auto; }
</style>
"""

_PDF_STYLE = """
<style>
@page { size: A4 landscape; margin: 1.3cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9pt; color: #1a1a1a; }
h1 { color: #2c5aa0; font-size: 18pt; }
h2 { color: #2c5aa0; font-size: 13pt; margin-top: 14pt; }
h3 { font-size: 10.5pt; margin-top: 8pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 7pt; }
th, td { border: 0.5pt solid #999; padding: 3pt 5pt; text-align: left; }
th { background-color: #2c5aa0; color: white; }
img { width: 480pt; }
</style>
"""


def _render_markdown(markdown_text: str) -> str:
    """Convert the report's Markdown body to an HTML fragment (tables + raw ``<br>`` preserved)."""
    return _markdown.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"])


def render_html(markdown_text: str, out_path: Path, *, title: str) -> None:
    """Render the report Markdown as a standalone, styled HTML page for easy local viewing."""
    body = _render_markdown(markdown_text)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{_HTML_STYLE}
</head>
<body>
{body}
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def render_pdf(markdown_text: str, out_path: Path, *, reports_dir: Path) -> bool:
    """Render the report Markdown as a PDF (landscape, to fit the wide appendix table).

    Images referenced by the Markdown as paths relative to ``reports_dir`` (e.g.
    ``histograms/foo.png``) are resolved to absolute filesystem paths via
    ``link_callback`` -- xhtml2pdf has no notion of "relative to this HTML
    file" the way a browser does.

    Returns:
        True on success. xhtml2pdf reports errors via a return code rather than
        raising, so callers should check this rather than assuming success.
    """
    body = _render_markdown(markdown_text)
    html = f"<html><head>{_PDF_STYLE}</head><body>{body}</body></html>"

    def link_callback(uri: str, _rel: str) -> str:
        if uri.startswith(("http://", "https://")):
            return uri
        return str((reports_dir / uri).resolve())

    with out_path.open("wb") as pdf_file:
        result = pisa.CreatePDF(html, dest=pdf_file, link_callback=link_callback)
    return not result.err


def write_report(
    *,
    out_path: Path,
    samples: list[Samples],
    histogram_paths: dict[tuple[str, str, int], Path],
    environment_md: str,
    methodology_md: str,
    interpretation_md: str,
    reproduction_md: str,
) -> str:
    """Assemble the report Markdown, write ``REPORT.md``, and return the Markdown text.

    The returned text is what the caller should feed to :func:`render_html` and
    :func:`render_pdf` -- all three artifacts come from this one string, so
    there is nothing to keep in sync between formats.
    """
    histograms_md_lines = ["## Histograms\n"]
    for (interface, operation, scale), path in sorted(histogram_paths.items()):
        rel = path.relative_to(out_path.parent)
        histograms_md_lines.append(f"**{interface}.{operation} @ scale={scale}**\n")
        histograms_md_lines.append(f"![{interface}.{operation}@{scale}]({rel})\n")

    content = f"""# strands-aerospike benchmark report

{environment_md}

## Methodology

{methodology_md}

## Summary tables

{_compact_tables(samples)}

{chr(10).join(histograms_md_lines)}

## Interpretation

{interpretation_md}

## Reproduction

{reproduction_md}

## Appendix: full raw statistics

{_appendix_table(samples)}
"""
    out_path.write_text(content, encoding="utf-8")
    return content
