#!/usr/bin/env python3
"""Render a clinician report from Markdown to a print-ready HTML file and PDF.

Why this exists rather than pandoc: the review skill runs on whatever machine the
user happens to be on, and health data must not leave it. A stdlib-only converter
plus headless Chrome (already present for other reasons) keeps the toolchain at
zero installs. The Markdown subset handled here is exactly what the clinician
report template uses -- headings, tables, bullets, bold/italic, rules. It is not
a general Markdown implementation and does not try to be.

Usage:
    render_report.py <report.md> [--no-pdf]

Writes <report>.html next to the input, then <report>.pdf if Chrome is found.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Chrome is the only PDF engine we can count on. Ordered by how likely the binary
# name is to exist on a given machine.
CHROME_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
* { box-sizing: border-box; }
body {
  font-family: "Source Sans 3", "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 9.6pt; line-height: 1.42; color: #1a1a1a; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1 {
  font-size: 17pt; line-height: 1.2; margin: 0 0 2mm 0; font-weight: 600;
  letter-spacing: -0.2pt;
}
h2 {
  font-size: 11.5pt; margin: 7mm 0 2.5mm 0; padding-bottom: 1.2mm;
  border-bottom: 1.2pt solid #1a1a1a; font-weight: 600;
  break-after: avoid; page-break-after: avoid;
}
h3 {
  font-size: 10pt; margin: 4.5mm 0 1.8mm 0; font-weight: 600; color: #333;
  break-after: avoid; page-break-after: avoid;
}
p { margin: 0 0 2.2mm 0; }
ul { margin: 0 0 2.5mm 0; padding-left: 4.5mm; }
li { margin: 0 0 1.4mm 0; break-inside: avoid; page-break-inside: avoid; }
strong { font-weight: 600; }
hr { border: 0; border-top: 0.5pt solid #c8c8c8; margin: 5mm 0; }

table {
  border-collapse: collapse; width: 100%; margin: 0 0 3mm 0;
  font-size: 8.8pt; break-inside: auto;
}
th {
  background: #f0f0f0; text-align: left; font-weight: 600;
  border: 0.5pt solid #b0b0b0; padding: 1.4mm 2mm;
}
td { border: 0.5pt solid #c8c8c8; padding: 1.3mm 2mm; vertical-align: top; }
tr { break-inside: avoid; page-break-inside: avoid; }
thead { display: table-header-group; }

/* The disclaimer line under the title, and the sign-off line at the end. */
.lede { font-size: 9.2pt; color: #333; margin: 0 0 4mm 0; }
.footer {
  margin-top: 6mm; padding-top: 2mm; border-top: 0.5pt solid #c8c8c8;
  font-size: 8.2pt; color: #555; font-style: italic;
}
"""

INLINE = (
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)"), r"<em>\1</em>"),
    (re.compile(r"`([^`]+?)`"), r"<code>\1</code>"),
)


def inline(text: str) -> str:
    """Escape a line, then apply the inline emphasis markers."""
    out = html.escape(text, quote=False)
    for pattern, repl in INLINE:
        out = pattern.sub(repl, out)
    return out


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_divider(line: str) -> bool:
    """True for the |---|---| line that marks the row above as a header."""
    cells = split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def convert(md: str) -> str:
    """Convert the report Markdown subset to an HTML body fragment."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    in_list = False
    # The first paragraph after the H1 is the disclaimer, and the last italic-only
    # line is the sign-off. Both get their own class for styling.
    seen_h1 = False
    seen_lede = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            close_list()
            i += 1
            continue

        # Table: a pipe row followed by a divider row.
        if stripped.startswith("|") and i + 1 < len(lines) and is_divider(lines[i + 1]):
            close_list()
            header = split_row(stripped)
            out.append("<table><thead><tr>")
            out.extend(f"<th>{inline(c)}</th>" for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                out.append("<tr>")
                out.extend(f"<td>{inline(c)}</td>" for c in split_row(lines[i]))
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        if stripped in ("---", "***", "___"):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            if level == 1:
                seen_h1 = True
            i += 1
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
            i += 1
            continue

        close_list()
        # A whole line wrapped in single asterisks is the sign-off.
        if re.fullmatch(r"\*[^*].*[^*]\*", stripped):
            out.append(f'<p class="footer">{inline(stripped)}</p>')
        elif seen_h1 and not seen_lede:
            out.append(f'<p class="lede">{inline(stripped)}</p>')
            seen_lede = True
        else:
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_list()
    return "\n".join(out)


def build_html(md_path: Path) -> Path:
    md = md_path.read_text(encoding="utf-8")
    title = next(
        (l.lstrip("# ").strip() for l in md.split("\n") if l.startswith("# ")),
        md_path.stem,
    )
    doc = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{CSS}</style>\n</head>\n"
        f"<body>\n{convert(md)}\n</body>\n</html>\n"
    )
    html_path = md_path.with_suffix(".html")
    html_path.write_text(doc, encoding="utf-8")
    return html_path


def build_pdf(html_path: Path) -> Path | None:
    chrome = next((shutil.which(b) for b in CHROME_BINARIES if shutil.which(b)), None)
    if not chrome:
        return None
    pdf_path = html_path.with_suffix(".pdf")
    # Chrome insists on a writable profile dir, and refuses to reuse a running
    # instance's one, so give it a throwaway.
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        sys.stderr.write(result.stderr[-2000:] + "\n")
        return None
    return pdf_path


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.stderr.write(__doc__ or "")
        return 2
    md_path = Path(args[0]).expanduser().resolve()
    if not md_path.is_file():
        sys.stderr.write(f"no such file: {md_path}\n")
        return 1

    html_path = build_html(md_path)
    print(f"html: {html_path}")

    if "--no-pdf" in sys.argv[1:]:
        return 0
    pdf_path = build_pdf(html_path)
    if pdf_path is None:
        sys.stderr.write(
            "pdf: FAILED (no Chrome binary found, or Chrome produced nothing).\n"
            "The HTML above is complete -- open it and print to PDF from the browser.\n"
        )
        return 1
    print(f"pdf:  {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
