"""The chat page's tiny markdown renderer (ole/static/render.js), run under node."""
import json
import subprocess
from pathlib import Path

JS = Path(__file__).parent.parent / "ole" / "static" / "render.js"
SOURCES = {"1": {"n": 1, "url": "https://wp.stolaf.edu/a/", "title": "A"},
           "2": {"n": 2, "url": "https://wp.stolaf.edu/b/", "title": "B"}}


def render(text):
    code = f"const {{render}} = require({json.dumps(str(JS))}); process.stdout.write(render({json.dumps(text)}, {json.dumps(SOURCES)}))"
    return subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout


def test_cite_on_own_line_joins_previous_item():
    html = render("1. Open Handshake\n2. Add a note\n\u00012\u0001\n\n**Next:** email.\u00011\u0001")
    assert "<p><a class=\"cite\"" not in html
    assert 'Add a note<a class="cite" href="https://wp.stolaf.edu/b/"' in html


def test_lists_bold_links_and_escaping():
    html = render("Hi <b>x</b>\n- **one** [site](https://wp.stolaf.edu/)\n- two")
    assert "&lt;b&gt;" in html and "<ul><li><strong>one</strong>" in html
    assert '<a href="https://wp.stolaf.edu/" target="_blank" rel="noopener">site</a>' in html


def test_bare_stolaf_domains_link():
    html = render("See catalog.stolaf.edu/academic-programs/computer-science/ or wp.stolaf.edu/pipercenter.")
    assert '<a href="https://catalog.stolaf.edu/academic-programs/computer-science/"' in html
    assert '<a href="https://wp.stolaf.edu/pipercenter"' in html
