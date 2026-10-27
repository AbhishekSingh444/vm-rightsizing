"""Render paper.md as IEEE conference-style HTML (two columns, 10pt Times).

Standard library only. The HTML is printed to PDF with headless Edge/Chrome:

    python paper/md2ieee.py paper/paper.md --out paper
    msedge --headless --no-pdf-header-footer --print-to-pdf=paper/paper.pdf paper/paper.html

Formatting only: section numbers become Roman numerals, subsections letters,
"§5.1" becomes "Section V-A", table captions "TABLE I" above the table, figure
captions "Fig. 1." below. Text is otherwise passed through unchanged.
"""
import argparse
import html
import re
from pathlib import Path

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def section_ref(m):
    parts = m.group(1).split(".")
    ref = ROMAN[int(parts[0])]
    if len(parts) > 1:
        ref += "-" + chr(ord("A") + int(parts[1]) - 1)
    return "Section " + ref


def make_inline(base):
    def inline(t):
        codes = []

        def keep(m):
            codes.append("<code>" + html.escape(m.group(1)) + "</code>")
            return f"\x00{len(codes)-1}\x00"
        t = re.sub(r"`([^`]+)`", keep, t)
        t = html.escape(t, quote=False)
        t = re.sub(r"§(\d+(?:\.\d+)?)", section_ref, t)

        def img(m):
            p = (base / m.group(2)).resolve().as_uri()
            return f'<img src="{p}" alt="{m.group(1)}">'
        t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img, t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
        return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], t)
    return inline


def parse(lines, inline):
    """Markdown lines -> list of (kind, payload) blocks."""
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", ln)
        if m:
            out.append(("h", (len(m.group(1)), m.group(2))))
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            h = "".join(f"<th>{inline(c)}</th>" for c in rows[0])
            b = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                        for r in rows[2:])
            out.append(("table", (len(rows[0]),
                                  f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>")))
            continue
        lm = re.match(r"(\d+\.|-)\s+", ln)
        if lm:
            tag = "ol" if lm.group(1)[0].isdigit() else "ul"
            items = []
            while i < len(lines):
                m2 = re.match(r"(\d+\.|-)\s+(.*)", lines[i])
                if m2:
                    items.append(m2.group(2))
                    i += 1
                elif lines[i].startswith("  ") and lines[i].strip() and items:
                    items[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            out.append((tag, items))
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||\d+\.\s|-\s)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append(("p", " ".join(buf)))
    return out


def render(blocks, inline):
    title, header, body = "", [], []
    sec = sub = 0
    in_abstract = in_refs = False
    pending = []

    def place(block, wide):
        # a full-width block straight after a heading strands the heading at a
        # column foot; float it below the next paragraph instead, as LaTeX would
        if pending or (wide and body and body[-1].startswith("<h")):
            pending.append(block)
        else:
            body.append(block)
    j = 0
    while j < len(blocks):
        kind, x = blocks[j]
        j += 1
        if kind == "h":
            level, text = x
            in_abstract = in_refs = False
            if level == 1:
                title = inline(text)
            elif text == "Abstract":
                in_abstract = True
            elif level == 2:
                m = re.match(r"(\d+)\s+(.*)", text)
                in_refs = text == "References"
                if m:
                    sec, sub = int(m.group(1)), 0
                    body.append(f"<h2>{ROMAN[sec]}. {inline(m.group(2))}</h2>")
                else:
                    body.append(f"<h2>{inline(text)}</h2>")
            else:
                m = re.match(r"\d+\.(\d+)\s+(.*)", text)
                sub = int(m.group(1)) if m else sub + 1
                body.append(f"<h3>{chr(ord('A') + sub - 1)}. {inline(m.group(2) if m else text)}</h3>")
            continue
        if not title:
            continue
        if not body and not in_abstract and kind == "p" and not header:
            # author line and draft note sit under the title
            header.append(f'<p class="authors">{inline(x)}</p>')
            continue
        if not body and not in_abstract and kind == "p":
            header.append(f'<p class="note">{inline(x)}</p>')
            continue
        if in_abstract:
            if kind == "p":
                lead = "<em>Abstract</em>&mdash;" if not any("abstract" in h for h in header) else ""
                header.append(f'<p class="abstract">{lead}{inline(x)}</p>')
            else:
                header.append(f'<{kind} class="abstract">' + "".join(f"<li>{inline(s)}</li>" for s in x) + f"</{kind}>")
            continue
        if in_refs and kind == "ol":
            body.append('<ol class="refs">' + "".join(
                f'<li><span class="rn">[{n}]</span>{inline(s)}</li>' for n, s in enumerate(x, 1)) + "</ol>")
            continue
        # table: caption paragraph, table, then trailing note/source paragraphs
        tm = re.match(r"\*\*Table (\d+)\. (.+?)\*\*\s*(.*)", x) if kind == "p" else None
        if tm and j < len(blocks) and blocks[j][0] == "table":
            ncols, tbl = blocks[j][1]
            j += 1
            cap = (f'<div class="tcap">TABLE {ROMAN[int(tm.group(1))]}<br>'
                   f'<span class="sc">{inline(tm.group(2))}</span>'
                   + (f' {inline(tm.group(3))}' if tm.group(3) else "") + "</div>")
            notes = []
            while j < len(blocks) and blocks[j][0] == "p" and re.match(r"(\*Sources|†)", blocks[j][1]):
                notes.append(f'<p class="tnote">{inline(blocks[j][1])}</p>')
                j += 1
            wide = " wide" if ncols > 3 else ""
            place(f'<div class="tbl{wide}">{cap}{tbl}{"".join(notes)}</div>', wide)
            continue
        if kind == "p" and re.fullmatch(r"(!\[[^\]]*\]\([^)]+\)\s*)+", x):
            imgs = inline(x)
            cap = ""
            if j < len(blocks) and blocks[j][0] == "p":
                fm = re.match(r"\*\*Figure (\d+)\.\*\*\s*(.*)", blocks[j][1])
                if fm:
                    cap = f"<figcaption>Fig. {fm.group(1)}. {inline(fm.group(2))}</figcaption>"
                    j += 1
            wide = " wide" if imgs.count("<img") > 1 else ""
            place(f'<figure class="fig{wide}">{imgs}{cap}</figure>', wide)
            continue
        if kind == "p":
            cls = ' class="src"' if x.startswith("*Sources") else ""
            body.append(f"<p{cls}>{inline(x)}</p>")
            body.extend(pending)
            pending.clear()
        elif kind == "table":
            body.append(x[1])
        else:
            body.append(f"<{kind}>" + "".join(f"<li>{inline(s)}</li>" for s in x) + f"</{kind}>")
    return title, header, body


CSS = """
@page { size: letter; margin: 0.75in 0.625in 1in 0.625in; }
html { font-family: 'Times New Roman', Times, serif; font-size: 10pt; line-height: 11.6pt; color: #000; }
body { margin: 0; background: #fff; }
.head { text-align: center; margin-bottom: 10pt; }
h1 { font-size: 24pt; font-weight: normal; line-height: 1.1; margin: 0 0 8pt; }
.authors { font-size: 11pt; margin: 0 0 4pt; }
.note { font-size: 8pt; margin: 0 0 8pt; }
.abstract { text-align: justify; font-size: 9pt; line-height: 10.4pt; font-weight: bold; margin: 0 0 3pt; text-indent: 1em; }
ul.abstract { text-indent: 0; padding-left: 2.2em; }
.abstract em:first-child { font-style: italic; }
.cols { column-count: 2; column-gap: 0.25in; column-fill: balance; text-align: justify; hyphens: auto; }
p { margin: 0; text-indent: 1em; }
h2 + p, h3 + p, .tbl + p, .fig + p, ul + p, ol + p { text-indent: 0; }
h2 { font-size: 10pt; font-weight: normal; font-variant: small-caps; text-align: center; margin: 9pt 0 4pt; break-after: avoid; }
h3 { font-size: 10pt; font-weight: normal; font-style: italic; margin: 6pt 0 3pt; break-after: avoid; }
ul, ol { margin: 2pt 0; padding-left: 1.6em; }
li { margin: 0 0 1pt; }
code { font-family: 'Courier New', Courier, monospace; font-size: 8.5pt; overflow-wrap: anywhere; }
.src, .tnote { font-size: 8pt; line-height: 9.4pt; text-indent: 0; margin: 2pt 0 4pt; text-align: left; }
.src code, .tnote code { font-size: 7.5pt; }
.tbl { break-inside: avoid; margin: 6pt 0; }
.tcap { font-size: 8pt; line-height: 9.6pt; text-align: center; margin-bottom: 3pt; }
.sc { font-variant: small-caps; }
table { border-collapse: collapse; width: 100%; font-size: 8pt; line-height: 9.2pt; text-align: left; hyphens: manual; }
th, td { border-top: 0.5pt solid #000; border-bottom: 0.5pt solid #000; padding: 1.5pt 3pt; vertical-align: top; }
thead th { border-top: 1pt solid #000; border-bottom: 1pt solid #000; font-weight: normal; font-variant: small-caps; }
tbody tr:last-child td { border-bottom: 1pt solid #000; }
td code { font-size: 7.5pt; }
.wide { column-span: all; }
.fig { margin: 6pt 0; text-align: center; break-inside: avoid; }
.fig img { max-width: 100%; }
.fig.wide img { width: 49%; }
figcaption { font-size: 8pt; line-height: 9.6pt; text-align: left; margin-top: 3pt; }
ol.refs { list-style: none; padding-left: 0; font-size: 8pt; line-height: 9.4pt; text-align: left; }
ol.refs li { position: relative; padding-left: 2.6em; margin-bottom: 2pt; }
ol.refs .rn { position: absolute; left: 0; }
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="Markdown source (paper/paper.md)")
    ap.add_argument("--out", type=Path, required=True, help="directory for paper.html")
    a = ap.parse_args()
    src = a.src.resolve()
    inline = make_inline(src.parent)
    text = re.sub(r"<!--.*?-->", "", src.read_text(encoding="utf-8"), flags=re.S)
    title, header, body = render(parse(text.splitlines(), inline), inline)
    a.out.mkdir(parents=True, exist_ok=True)
    dest = a.out / (src.stem + ".html")
    dest.write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{src.stem}</title><style>{CSS}</style></head><body>"
        f"<div class='head'><h1>{title}</h1>{''.join(header)}</div>"
        f"<div class='cols'>{''.join(body)}</div></body></html>", encoding="utf-8")
    print(dest)


if __name__ == "__main__":
    main()
