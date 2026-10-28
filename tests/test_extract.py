from pathlib import Path

from ole.extract import extract_html, extract_links, extract_pdf, extract_text

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text()


def test_catalog_courseblocks():
    title, secs = extract_html(read("catalog_cs.html"), "https://catalog.stolaf.edu/academic-programs/computer-science/")
    assert title == "Computer Science"
    course = [s for s in secs if "CSCI 251" in s.heading_path]
    assert len(course) == 1 and len(course[0].text) > 50
    assert "\xa0" not in course[0].heading_path


def test_wp_heading_paths():
    title, secs = extract_html(read("wp_page.html"), "https://wp.stolaf.edu/pipercenter/")
    assert title == "Piper Center for Vocation and Career"
    assert secs and all(s.text.strip() for s in secs)


def test_heading_stack():
    html = "<html><head><title>T</title></head><body><main><h1>Intern</h1><p>" + "word " * 60 + \
           "</p><h2>Funding</h2><p>" + "money " * 60 + "</p></main></body></html>"
    _, secs = extract_html(html, "https://wp.stolaf.edu/x/")
    assert any(s.heading_path == "Intern > Funding" and "money" in s.text for s in secs)


def test_extract_links_and_google():
    links = extract_links('<a href="/registrar/">r</a><a href="mailto:x@y">m</a>'
                          '<a href="https://docs.google.com/document/d/ABC/edit">d</a>'
                          '<a href="#top">t</a>', "https://wp.stolaf.edu/a/")
    assert "https://wp.stolaf.edu/registrar/" in links
    assert not any(l.startswith("mailto") for l in links)
    assert any("docs.google.com/document/d/ABC" in l for l in links)


def test_empty_pdf_gives_no_sections():
    from io import BytesIO
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(100, 100)
    buf = BytesIO()
    w.write(buf)
    title, secs = extract_pdf(buf.getvalue(), "https://wp.stolaf.edu/wp-content/uploads/x/form.pdf")
    assert secs == [] and title == "form.pdf"


def test_extract_text():
    title, secs = extract_text("Hello\n\nWorld", "Doc")
    assert title == "Doc" and secs[0].text.startswith("Hello")


def test_catalog_tabs_all_extracted():
    _, secs = extract_html(read("catalog_cs.html"), "https://catalog.stolaf.edu/academic-programs/computer-science/")
    blob = "\n".join(s.heading_path + "\n" + s.text for s in secs)
    assert "Requirements for the Major" in blob
    assert "Plan of Study" in blob
    assert "Ryota Matsuura" in blob
