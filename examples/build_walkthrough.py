"""Build the walkthrough notebook and page from examples/walkthrough.py.

    python examples/build_walkthrough.py --record
        execute the notebook in a Jupyter kernel (needs models, Neo4j and the
        [notebook] extra), then render the page
    python examples/build_walkthrough.py
        render site/walkthrough.html from the executed notebook

The script is the source; examples/walkthrough.ipynb is the script's cells plus the outputs of one
real run, and the page shows exactly that notebook. Editing the script without re-recording fails
the build instead of publishing outputs the code no longer produces.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import html
import importlib
import io
import json
import keyword
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "walkthrough.py"
NOTEBOOK = ROOT / "examples" / "walkthrough.ipynb"
PAGE = ROOT / "site" / "walkthrough.html"
REPO = "https://github.com/sjortiz/nexo/blob/main/"


@dataclass(frozen=True)
class Cell:
    """One cell of the percent-format script."""

    kind: str  # "markdown" or "code"
    source: str

    @property
    def key(self) -> str:
        """Short content hash, used as the notebook cell id."""
        return hashlib.sha256(self.source.encode()).hexdigest()[:16]


def parse_cells(text: str) -> list[Cell]:
    """Split a percent-format script into cells; text before the first cell is ignored."""
    cells: list[Cell] = []
    kind, lines = None, []

    def flush() -> None:
        if kind is not None:
            source = "\n".join(lines).strip("\n")
            if source:
                cells.append(Cell(kind, source))

    for line in text.splitlines():
        if line.startswith("# %%"):
            flush()
            kind, lines = ("markdown" if "[markdown]" in line else "code"), []
        elif kind == "markdown":
            lines.append(line[2:] if line.startswith("# ") else line.lstrip("#"))
        else:
            lines.append(line)
    flush()
    return cells


# --- notebook ------------------------------------------------------------------------------
def notebook_skeleton(cells: list[Cell]) -> dict:
    """An unexecuted nbformat 4.5 notebook with stable cell ids."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "name": "python3",
                "display_name": "Python 3",
                "language": "python",
            },
            "language_info": {"name": "python"},
        },
        "cells": [
            {
                "id": f"{cell.kind[:2]}-{cell.key}",
                "cell_type": cell.kind,
                "metadata": {},
                "source": cell.source,
                **(
                    {"execution_count": None, "outputs": []}
                    if cell.kind == "code"
                    else {}
                ),
            }
            for cell in cells
        ],
    }


def record() -> None:
    """Execute the script's cells in a Jupyter kernel and save the notebook with its outputs."""
    try:  # optional extra: nexo[notebook]
        nbformat = importlib.import_module("nbformat")
        nbclient = importlib.import_module("nbclient")
    except ModuleNotFoundError as exc:
        raise SystemExit("--record needs the extra: nexo[notebook]") from exc

    notebook = nbformat.from_dict(notebook_skeleton(parse_cells(SCRIPT.read_text())))
    # Start where Jupyter users start: in examples/. Any cell error aborts the recording.
    nbclient.NotebookClient(
        notebook,
        timeout=1800,
        kernel_name="python3",
        record_timing=False,
        resources={"metadata": {"path": str(NOTEBOOK.parent)}},
    ).execute()
    nbformat.write(notebook, NOTEBOOK)


def _text(source) -> str:
    return "".join(source) if isinstance(source, list) else source


def cell_output(cell: dict) -> str:
    """What a reader sees under a code cell: stdout and results; errors are never publishable."""
    parts = []
    for output in cell.get("outputs", []):
        if output["output_type"] == "error":
            raise SystemExit(
                f"cell {cell['id']} failed in the recorded run: {output['ename']}"
            )
        if output["output_type"] == "stream" and output["name"] == "stdout":
            parts.append(_text(output["text"]))
        elif output["output_type"] in ("execute_result", "display_data"):
            parts.append(_text(output["data"].get("text/plain", "")) + "\n")
    return "".join(parts).rstrip()


def sync_prose(cells: list[Cell]) -> None:
    """Copy the script's prose into the recorded notebook; code cells must be unchanged."""
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    recorded = notebook["cells"]
    same_code = [c["cell_type"] for c in recorded] == [c.kind for c in cells] and all(
        _text(r["source"]) == c.source
        for r, c in zip(recorded, cells, strict=True)
        if c.kind == "code"
    )
    if not same_code:
        raise SystemExit(
            "code cells changed since the recorded run; "
            "run: python examples/build_walkthrough.py --record"
        )
    for record_cell, cell in zip(recorded, cells, strict=True):
        if cell.kind == "markdown":
            record_cell["source"] = cell.source.splitlines(keepends=True)
    # nbformat's on-disk layout, so a prose sync leaves no other diff.
    NOTEBOOK.write_text(
        json.dumps(notebook, sort_keys=True, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_notebook(cells: list[Cell]) -> dict:
    """The recorded notebook, which must hold exactly the script's cells."""
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    recorded = [(c["cell_type"], _text(c["source"])) for c in notebook["cells"]]
    if recorded != [(c.kind, c.source) for c in cells]:
        raise SystemExit(
            "examples/walkthrough.ipynb does not match examples/walkthrough.py; "
            "run: python examples/build_walkthrough.py --record"
        )
    return notebook


# --- rendering -----------------------------------------------------------------------------
_EMPHASIS = [
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\w*])\*([^*]+)\*(?![\w*])"), r"<em>\1</em>"),
]
_LINK = re.compile(r"\[((?:`[^`]+`|[^\]`])+)\]\(([^)\s]+)\)")


def _spans(text: str) -> str:
    """Code spans and emphasis on already-escaped text; nothing is formatted inside code."""
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{part[1:-1]}</code>")
        else:
            for pattern, replacement in _EMPHASIS:
                part = pattern.sub(replacement, part)
            out.append(part)
    return "".join(out)


def _inline(text: str) -> str:
    links: list[tuple[str, str]] = []

    def stash(match: re.Match) -> str:  # links first, so their text may hold code spans
        links.append((match.group(1), match.group(2)))
        return f"\x00{len(links) - 1}\x00"

    out = _spans(_LINK.sub(stash, html.escape(text, quote=False)))
    return re.sub(
        r"\x00(\d+)\x00",
        lambda m: f'<a href="{links[int(m.group(1))][1]}">{_spans(links[int(m.group(1))][0])}</a>',
        out,
    )


def slug(heading: str) -> str:
    """URL fragment for a heading."""
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def render_markdown(source: str) -> str:
    """HTML for the markdown subset the walkthrough uses: headings, lists and paragraphs."""
    blocks, out = re.split(r"\n\s*\n", source.strip()), []
    for block in blocks:
        lines = block.splitlines()
        if block.startswith("# "):
            out.append(f"<h1>{_inline(block[2:].strip())}</h1>")
        elif block.startswith("## "):
            title = block[3:].strip()
            out.append(f'<h2 id="{slug(title)}">{_inline(title)}</h2>')
        elif lines[0].startswith("- "):
            items: list[str] = []
            for line in lines:
                if line.startswith("- "):
                    items.append(line[2:].strip())
                else:
                    items[-1] += " " + line.strip()
            out.append(
                "<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>"
            )
        else:
            out.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
    return "\n".join(out)


_BUILTINS = set(dir(builtins))
_STRING_TOKENS = {
    tokenize.STRING,
    *(
        getattr(tokenize, n)
        for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if hasattr(tokenize, n)
    ),
}


def highlight(source: str) -> str:
    """Python source as HTML with token classes; whitespace preserved exactly."""
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    out, position, previous = [], 0, ""
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        start = offsets[tok.start[0] - 1] + tok.start[1]
        end = offsets[tok.end[0] - 1] + tok.end[1]
        if start < position or tok.type in (tokenize.ENDMARKER, tokenize.DEDENT):
            continue
        out.append(html.escape(source[position:start], quote=False))
        text = html.escape(source[start:end], quote=False)
        css = None
        if tok.type == tokenize.COMMENT:
            css = "cm"
        elif tok.type in _STRING_TOKENS:
            css = "st"
        elif tok.type == tokenize.NUMBER:
            css = "nu"
        elif tok.type == tokenize.NAME:
            if keyword.iskeyword(tok.string):
                css = "kw"
            elif previous in ("def", "class"):
                css = "fn"
            elif tok.string in _BUILTINS:
                css = "bi"
        out.append(f'<span class="{css}">{text}</span>' if css else text)
        position = end
        if tok.type == tokenize.NAME:
            previous = tok.string
        elif tok.type not in (
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.COMMENT,
        ):
            previous = ""
    out.append(html.escape(source[position:], quote=False))
    return "".join(out)


def render() -> str:
    """The walkthrough page: the script's prose and code with the recorded outputs."""
    cells = parse_cells(SCRIPT.read_text())
    notebook = load_notebook(cells)
    toc, body = [], []
    for cell, recorded in zip(cells, notebook["cells"], strict=True):
        if cell.kind == "markdown":
            for title in re.findall(r"^## (.+)$", cell.source, re.MULTILINE):
                toc.append(f'<li><a href="#{slug(title)}">{_inline(title)}</a></li>')
            body.append(f'<div class="prose">{render_markdown(cell.source)}</div>')
        else:
            block = f'<pre class="code"><code>{highlight(cell.source)}</code></pre>'
            if output := cell_output(recorded):
                block += (
                    f'<div class="out-label">Output</div>'
                    f'<pre class="output"><samp>{html.escape(output, quote=False)}</samp></pre>'
                )
            body.append(f'<div class="cell">{block}</div>')
    return TEMPLATE.format(
        toc="\n".join(toc),
        body="\n".join(body),
        script=f"{REPO}examples/walkthrough.py",
        notebook=f"{REPO}examples/walkthrough.ipynb",
        icon=ICON,
    )


ICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Ccircle cx='8' cy='8' r='5' fill='%235b4bdb'/%3E"
    "%3Ccircle cx='24' cy='12' r='5' fill='%235b4bdb'/%3E"
    "%3Ccircle cx='14' cy='25' r='5' fill='%235b4bdb'/%3E"
    "%3Cpath d='M8 8 24 12 14 25Z' fill='none' stroke='%235b4bdb' stroke-width='2'/%3E%3C/svg%3E"
)

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo walkthrough</title>
<link rel="icon" href="{icon}">
<meta name="description"
      content="Build a knowledge graph from two documents and answer a multi-hop question with Nexo, step by step.">
<!-- Generated by examples/build_walkthrough.py from examples/walkthrough.py; do not edit by hand. -->
<style>
  :root {{
    --bg: #fbfbfd; --surface: #ffffff; --surface-2: #f3f2fa; --text: #1b1a24; --muted: #5a586b;
    --border: #e3e1ef; --accent: #5b4bdb; --accent-soft: #ece9ff; --code-bg: #f6f5fb; --out-bg: #ffffff;
    --kw: #7c3aed; --st: #0f7b4b; --cm: #8a879c; --nu: #b45309; --fn: #2563eb; --bi: #be185d;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #111018; --surface: #191823; --surface-2: #221f30; --text: #ecebf5; --muted: #a9a6bd;
      --border: #2e2b40; --accent: #9d92ff; --accent-soft: #2a2545; --code-bg: #1d1b29; --out-bg: #15141e;
      --kw: #c4b5fd; --st: #86efac; --cm: #8d8aa3; --nu: #fbbf24; --fn: #93c5fd; --bi: #f9a8d4;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
         font: 16px/1.65 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }}
  a {{ color: var(--accent); }}
  header.top {{ position: sticky; top: 0; z-index: 10; border-bottom: 1px solid var(--border);
               background: color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter: blur(8px); }}
  header.top .bar {{ max-width: 1180px; margin: 0 auto; padding: 0 20px; height: 56px; display: flex;
                    align-items: center; justify-content: space-between; gap: 16px; }}
  .brand {{ font-weight: 700; color: var(--text); text-decoration: none; }}
  header nav a {{ color: var(--muted); text-decoration: none; margin-left: 18px; font-size: .95rem; }}
  .layout {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; display: grid;
            grid-template-columns: 230px minmax(0, 1fr); gap: 48px; }}
  aside {{ position: sticky; top: 80px; align-self: start; font-size: .9rem; }}
  aside h2 {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
             margin: 0 0 10px; }}
  aside ol {{ list-style: none; padding: 0; margin: 0; }}
  aside li {{ margin: 0 0 6px; }}
  aside a {{ color: var(--muted); text-decoration: none; }}
  aside a:hover {{ color: var(--text); }}
  main {{ min-width: 0; }}
  .prose h1 {{ font-size: clamp(2rem, 4vw, 2.8rem); line-height: 1.15; margin: 0 0 16px; letter-spacing: -.02em; }}
  .prose h2 {{ font-size: 1.5rem; margin: 48px 0 8px; padding-top: 16px; border-top: 1px solid var(--border);
              scroll-margin-top: 72px; }}
  .prose p, .prose li {{ color: var(--text); }}
  .prose code, .out-label + pre code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .prose code {{ background: var(--surface-2); padding: 1px 6px; border-radius: 6px; font-size: .88em; }}
  .cell {{ margin: 16px 0 24px; }}
  pre {{ margin: 0; overflow-x: auto; padding: 14px 16px; font: .85rem/1.55 ui-monospace, SFMono-Regular, Menlo,
        Consolas, monospace; border: 1px solid var(--border); }}
  pre.code {{ background: var(--code-bg); border-radius: 12px 12px 0 0; }}
  pre.code:last-child {{ border-radius: 12px; }}
  .out-label {{ font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
               background: var(--surface-2); border: 1px solid var(--border); border-top: 0; padding: 4px 16px; }}
  pre.output {{ background: var(--out-bg); border-top: 0; border-radius: 0 0 12px 12px; color: var(--text);
               max-height: 520px; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .kw {{ color: var(--kw); }} .st {{ color: var(--st); }} .cm {{ color: var(--cm); font-style: italic; }}
  .nu {{ color: var(--nu); }} .fn {{ color: var(--fn); }} .bi {{ color: var(--bi); }}
  footer {{ border-top: 1px solid var(--border); color: var(--muted); font-size: .9rem; }}
  footer div {{ max-width: 1180px; margin: 0 auto; padding: 24px 20px 40px; }}
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: minmax(0, 1fr); gap: 0; padding-top: 20px; }}
    aside {{ position: static; margin-bottom: 8px; }}
    aside ol {{ columns: 2; }}
  }}
</style>
</head>
<body>
<header class="top">
  <div class="bar">
    <a class="brand" href="index.html">Nexo</a>
    <nav aria-label="Site"><a href="index.html">Overview</a><a href="index.html#quickstart">Run it</a><a
      href="{notebook}">Notebook</a><a href="https://github.com/sjortiz/nexo">GitHub</a></nav>
  </div>
</header>
<div class="layout">
  <aside>
    <h2>Steps</h2>
    <ol>
{toc}
    </ol>
  </aside>
  <main id="top">
{body}
  </main>
</div>
<footer><div>Generated from <a href="{script}">examples/walkthrough.py</a> and the outputs of a real run of
  <a href="{notebook}">examples/walkthrough.ipynb</a>.
  Nexo · <a href="https://github.com/sjortiz/nexo/blob/main/LICENSE">MIT License</a></div></footer>
</body>
</html>
"""


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--record", action="store_true", help="run every cell and save its output first"
    )
    args = parser.parse_args()
    cells = parse_cells(SCRIPT.read_text())
    if args.record:
        record()
    else:
        sync_prose(cells)
    PAGE.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
