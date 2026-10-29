from ole.chunk import chunk_sections
from ole.extract import Section

URL = "https://wp.stolaf.edu/x/"


def words(n, w="w"):
    return " ".join(f"{w}{i}" for i in range(n))


def test_split_with_overlap():
    chunks = chunk_sections(URL, "T", [Section("H", words(700))], None, "html")
    assert len(chunks) == 3
    a, b = chunks[0].text.split(), chunks[1].text.split()
    assert len(a) == 300 and a[-50:] == b[:50]
    assert chunks[-1].text.split()[-1] == "w699"


def test_short_sections_merged():
    secs = [Section("A", words(10, "short")), Section("A > B", words(100, "long"))]
    chunks = chunk_sections(URL, "T", secs, None, "html")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "A > B"
    assert "short0" in chunks[0].text and "long99" in chunks[0].text


def test_ids_deterministic():
    secs = [Section("H", words(400))]
    ids1 = [c.id for c in chunk_sections(URL, "T", secs, None, "html")]
    ids2 = [c.id for c in chunk_sections(URL, "T", secs, None, "html")]
    assert ids1 == ids2 and len(set(ids1)) == len(ids1)


def test_embed_text_has_context():
    c = chunk_sections(URL, "Piper Center", [Section("Internships > Funding", words(60))], "2025-01-01", "html")[0]
    assert "Piper Center" in c.embed_text and "Internships > Funding" in c.embed_text
    assert c.lastmod == "2025-01-01" and c.kind == "html" and c.url == URL


def test_newlines_kept():
    c = chunk_sections(URL, "T", [Section("H", "| a | b |\n| c | d |")], None, "html")[0]
    assert c.text == "| a | b |\n| c | d |"


def test_folded_heading_stays_with_its_own_text():
    # each folded short section's heading goes right above its own text, not above the text carried before it
    secs = [Section("Stav Hall", "Meal Swipe"), Section("Dinner", "4:30-8 p.m."),
            Section("Dinner > The Cage", "Flex Dollars"), Section("Other", words(60))]
    text = chunk_sections(URL, "T", secs, None, "html")[0].text
    assert text.startswith("Stav Hall\nMeal Swipe")
    assert text.index("Dinner\n4:30-8") < text.index("The Cage\nFlex Dollars") < text.index("Other\nw0")
