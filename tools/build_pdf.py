"""Build a single submission PDF from the markdown docs.

Renders README + DECISIONS + the architecture notes into one styled HTML file, then
asks a headless browser to print it to PDF. Kept in the repository so the PDF is
reproducible rather than a file nobody can regenerate.

Usage:
    python tools/build_pdf.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import markdown

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "build"
HTML_PATH = OUT_DIR / "submission.html"
PDF_PATH = REPO / "TrackBox-Submission.pdf"

DOCS = [
    ("README.md", None),
    ("DECISIONS.md", None),
    ("docs/ARCHITECTURE.md", None),
]

CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.55; color: #1a1a1a; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1 { font-size: 20pt; margin: 0 0 4mm; padding-bottom: 2mm;
     border-bottom: 2.5px solid #111; page-break-before: always; page-break-after: avoid; }
h1:first-of-type { page-break-before: avoid; }
h2 { font-size: 14pt; margin: 7mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1px solid #d5d5d5; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 5mm 0 1.5mm; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 4mm 0 1mm; page-break-after: avoid; }
p, ul, ol, blockquote { margin: 0 0 2.5mm; }
ul, ol { padding-left: 6mm; }
li { margin-bottom: 1mm; }
code { font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
       font-size: 9.2pt; background: #f2f2f2; padding: 0.5mm 1mm; border-radius: 2px; }
pre { background: #f7f7f7; border: 1px solid #e0e0e0; border-left: 3px solid #888;
      border-radius: 3px; padding: 2.5mm 3mm; overflow-x: auto;
      page-break-inside: avoid; margin: 0 0 3mm; }
pre code { background: none; padding: 0; font-size: 8.8pt; line-height: 1.4; }
table { border-collapse: collapse; width: 100%; margin: 0 0 3mm;
        font-size: 9.5pt; page-break-inside: avoid; }
th, td { border: 1px solid #d0d0d0; padding: 1.5mm 2mm; text-align: left; vertical-align: top; }
th { background: #f0f0f0; font-weight: 600; }
blockquote { border-left: 3px solid #bbb; padding-left: 3mm; color: #444; }
hr { border: none; border-top: 1px solid #ddd; margin: 6mm 0; }
a { color: #0b57d0; text-decoration: none; }
.cover { text-align: center; padding-top: 55mm; page-break-after: always; }
.cover h1 { border: none; font-size: 26pt; page-break-before: avoid; margin-bottom: 3mm; }
.cover .sub { font-size: 13pt; color: #444; margin-bottom: 10mm; }
.cover .box { display: inline-block; text-align: left; border: 1px solid #ccc;
              border-radius: 4px; padding: 5mm 7mm; font-size: 10.5pt; }
.cover .box div { margin-bottom: 1.5mm; }
.doc-title { font-size: 9pt; letter-spacing: 1.5px; text-transform: uppercase;
             color: #888; margin-bottom: 2mm; page-break-before: always; }
.doc-title:first-of-type { page-break-before: avoid; }
"""

COVER = """
<div class="cover">
  <h1>Automated Pitch Boundary<br/>&amp; Crop Engine</h1>
  <div class="sub">Software / Backend Developer Challenge</div>
  <div class="box">
    <div><strong>What it is:</strong> a pipeline that reads match video, finds the
      pitch boundary, and reports the run to the platform</div>
    <div><strong>Language:</strong> Python 3.11+ (container uses 3.12)</div>
    <div><strong>Tests:</strong> 176 passing</div>
    <div><strong>Runs in:</strong> Docker, reporting over the network</div>
  </div>
</div>
"""


def find_browser() -> str | None:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def main() -> int:
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "toc"])

    parts = [COVER]
    for name, _ in DOCS:
        path = REPO / name
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 1
        md.reset()
        parts.append(f'<div class="doc-title">{name}</div>')
        parts.append(md.convert(path.read_text(encoding="utf-8")))

    OUT_DIR.mkdir(exist_ok=True)
    HTML_PATH.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>TrackBox Submission</title><style>{CSS}</style></head>"
        f"<body>{''.join(parts)}</body></html>",
        encoding="utf-8",
    )
    print(f"wrote {HTML_PATH}")

    browser = find_browser()
    if not browser:
        print("No headless browser found. Open the HTML and print it to PDF.", file=sys.stderr)
        return 1

    result = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--virtual-time-budget=10000",
            f"--print-to-pdf={PDF_PATH}",
            HTML_PATH.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if not PDF_PATH.exists():
        print(result.stdout[-2000:], result.stderr[-2000:], file=sys.stderr)
        return 1

    print(f"wrote {PDF_PATH} ({PDF_PATH.stat().st_size / 1024:.0f} KB, {result.returncode=})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
