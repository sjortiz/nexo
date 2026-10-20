"""The walkthrough page is generated from examples/walkthrough.py and a recorded run; keep them in sync."""

from __future__ import annotations

import ast
import html
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
import build_walkthrough as bw  # noqa: E402

SCRIPT_TEXT = bw.SCRIPT.read_text()
CELLS = bw.parse_cells(SCRIPT_TEXT)
CODE = [c for c in CELLS if c.kind == "code"]


def test_script_alternates_prose_and_code():
    assert CELLS[0].kind == "markdown" and CELLS[0].source.startswith("# ")
    assert len(CODE) >= 15
    headings = re.findall(r"^## (.+)$", "\n".join(c.source for c in CELLS if c.kind == "markdown"), re.MULTILINE)
    numbered = [h for h in headings if re.match(r"\d+\. ", h)]
    assert [int(h.split(".")[0]) for h in numbered] == list(range(1, len(numbered) + 1)), "steps out of order"
    for index, cell in enumerate(CODE):
        compile(cell.source, f"<cell {index}>", "exec")


def test_published_page_matches_script_and_recorded_run():
    assert bw.PAGE.read_text(encoding="utf-8") == bw.render(), "run: python examples/build_walkthrough.py"


def test_notebook_is_the_script_executed_once_in_order():
    notebook = bw.load_notebook(CELLS)  # raises if the cells differ from the script
    code = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert [c["execution_count"] for c in code] == list(range(1, len(code) + 1)), "run the notebook top to bottom"
    outputs = [bw.cell_output(c) for c in code]  # raises on an error output
    assert sum(map(bool, outputs)) >= len(code) - 3  # only setup cells print nothing
    assert notebook["metadata"]["kernelspec"]["name"] == "python3"


def test_editing_a_cell_without_recording_fails(monkeypatch, tmp_path):
    edited = tmp_path / "walkthrough.py"
    edited.write_text(bw.SCRIPT.read_text().replace("chunk_size=settings.chunk_size", "chunk_size=400", 1))
    monkeypatch.setattr(bw, "SCRIPT", edited)
    with pytest.raises(SystemExit, match="--record"):
        bw.render()


def test_prose_edits_sync_without_rerunning_and_code_edits_do_not(monkeypatch, tmp_path):
    notebook, script = tmp_path / "walkthrough.ipynb", tmp_path / "walkthrough.py"
    notebook.write_bytes(bw.NOTEBOOK.read_bytes())
    monkeypatch.setattr(bw, "NOTEBOOK", notebook)
    monkeypatch.setattr(bw, "SCRIPT", script)
    before = [bw.cell_output(c) for c in json.loads(notebook.read_text())["cells"] if c["cell_type"] == "code"]

    script.write_text(SCRIPT_TEXT.replace("## Setup", "## Set up", 1))
    bw.sync_prose(bw.parse_cells(script.read_text()))
    synced = json.loads(notebook.read_text())
    assert "## Set up" in "".join("".join(c["source"]) for c in synced["cells"])
    assert [bw.cell_output(c) for c in synced["cells"] if c["cell_type"] == "code"] == before
    bw.load_notebook(bw.parse_cells(script.read_text()))  # now consistent

    script.write_text(script.read_text().replace("chunk_size=settings.chunk_size", "chunk_size=400", 1))
    with pytest.raises(SystemExit, match="--record"):
        bw.sync_prose(bw.parse_cells(script.read_text()))


def test_error_outputs_are_never_published():
    cell = {"id": "co-x", "outputs": [{"output_type": "stream", "name": "stdout", "text": ["ok\n"]},
                                      {"output_type": "error", "ename": "KeyError", "evalue": "", "traceback": []}]}
    with pytest.raises(SystemExit, match="KeyError"):
        bw.cell_output(cell)
    stderr_only = {"id": "co-y", "outputs": [{"output_type": "stream", "name": "stderr", "text": "warn"}]}
    assert bw.cell_output(stderr_only) == ""


def test_skeleton_is_a_valid_notebook():
    nbformat = pytest.importorskip("nbformat")
    nbformat.validate(nbformat.from_dict(bw.notebook_skeleton(CELLS)))


def test_walkthrough_uses_real_nexo_names():
    for node in ast.walk(ast.parse(bw.SCRIPT.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexo"):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name}"


def test_corpus_documents_exist_for_every_source_id():
    source_ids = re.findall(r'source_id="([^"]+)"', bw.SCRIPT.read_text())
    assert len(source_ids) == 2
    for source_id in source_ids:
        path = ROOT / "examples" / "corpus" / f"{source_id}.md"
        assert path.is_file(), source_id
        # A broad ignore rule (e.g. corpus/) would keep the examples out of every clone.
        ignored = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT, check=False)
        assert ignored.returncode == 1, f"{path} is git-ignored"


@pytest.mark.parametrize("cell", CODE, ids=lambda c: c.key)
def test_highlighting_preserves_the_source_exactly(cell):
    rendered = bw.highlight(cell.source)
    assert html.unescape(re.sub(r"</?span[^>]*>", "", rendered)) == cell.source


def test_highlighting_marks_tokens():
    out = bw.highlight('def show(value):  # note\n    return len("x") + 1\n')
    assert '<span class="kw">def</span> <span class="fn">show</span>' in out
    assert '<span class="cm"># note</span>' in out
    assert '<span class="bi">len</span>' in out and '<span class="st">"x"</span>' in out
    assert '<span class="nu">1</span>' in out


def test_markdown_subset():
    out = bw.render_markdown("## 2. Classify\n\nUse `a**b**c` and **bold** and *em* "
                             "[docs](https://x.example/y).\n\n- one\n  continued\n- two <b>")
    assert '<h2 id="2-classify">2. Classify</h2>' in out
    assert "<code>a**b**c</code>" in out  # no formatting inside code
    assert "<strong>bold</strong>" in out and "<em>em</em>" in out
    assert '<a href="https://x.example/y">docs</a>' in out
    assert "<li>one continued</li>" in out and "<li>two &lt;b&gt;</li>" in out
    linked_code = bw.render_markdown("Open [`a/b.ipynb`](https://x.example/a/b.ipynb) in Jupyter.")
    assert '<a href="https://x.example/a/b.ipynb"><code>a/b.ipynb</code></a>' in linked_code
