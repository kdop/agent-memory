#!/usr/bin/env python3
"""Generate a shields-style coverage badge SVG — stdlib only, no third-party deps.

Reads the total coverage percentage from coverage.py (`coverage report
--format=total`) and writes a flat SVG badge. Pass `--percent N` to skip reading
coverage data (used to seed the initial committed badge).

    python scripts/coverage_badge.py [-o coverage.svg] [--percent 86]

Kept stdlib-only on purpose: the memory client surface takes no third-party deps,
and `coverage-badge` drags in a deprecated `pkg_resources` import (breaks on 3.12+).
"""
import argparse
import subprocess
import sys

# (min_percent, color) — first match wins; classic shields thresholds.
_COLORS = [(95, "#4c1"), (90, "#97ca00"), (80, "#a4a61d"),
           (70, "#dfb317"), (60, "#fe7d37")]
_RED = "#e05d44"


def read_total():
    """Total coverage percent from the current coverage data, as an int."""
    out = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--format=total"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"coverage report failed:\n{out.stderr.strip()}")
    return round(float(out.stdout.strip()))


def color_for(pct):
    for threshold, c in _COLORS:
        if pct >= threshold:
            return c
    return _RED


def _text_width(s):
    # ~7px/char at 11px DejaVu; good enough for a stable, readable badge.
    return 7 * len(s) + 10


def render(pct):
    label, value = "coverage", f"{pct}%"
    lw, vw = _text_width(label), _text_width(value)
    total = lw + vw
    fill = color_for(pct)
    # Text is centered in each half; *10 scale matches the transform below.
    lx, vx = lw * 5, lw * 10 + vw * 5
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="coverage: {value}">
  <title>coverage: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{lw}" height="20" fill="#555"/>
    <rect x="{lw}" width="{vw}" height="20" fill="{fill}"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="110" text-rendering="geometricPrecision">
    <text aria-hidden="true" x="{lx}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{(lw - 10) * 10}">{label}</text>
    <text x="{lx}" y="140" transform="scale(.1)" textLength="{(lw - 10) * 10}">{label}</text>
    <text aria-hidden="true" x="{vx}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{(vw - 10) * 10}">{value}</text>
    <text x="{vx}" y="140" transform="scale(.1)" textLength="{(vw - 10) * 10}">{value}</text>
  </g>
</svg>
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default="coverage.svg")
    ap.add_argument("--percent", type=int, help="use this percent instead of reading coverage data")
    args = ap.parse_args()
    pct = args.percent if args.percent is not None else read_total()
    with open(args.output, "w") as f:
        f.write(render(pct))
    print(f"wrote {args.output} ({pct}%)")


if __name__ == "__main__":
    main()
