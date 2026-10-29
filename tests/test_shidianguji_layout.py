"""Page markers and character boxes from 识典古籍 reader data (offline fixture)."""
import json
from pathlib import Path

import pytest

from bookget.adapters.other.shidianguji import ShidianGujiAdapter
from bookget.text_converters import MarkdownConverter, PlainTextConverter
from bookget.text_parsers.shidianguji_parser import ShidianGujiParser

FIXTURE = Path(__file__).parent / "fixtures" / "shidianguji_SBCK011_sample.json"


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _chapter(fx, page_nums=None):
    meta = {"title": "論語", "page_nums": page_nums or {}}
    return ShidianGujiParser().parse(fx["paragraphs"], "SBCK011", "u", meta).chapters[0]


def test_page_starts_match_first_char_of_each_pages_boxes(fx):
    ch = _chapter(fx)
    starts = {b["page"]: ch["paragraphs"][b["para"]][b["offset"]:] for b in ch["page_starts"]}
    assert sorted(starts) == [1, 2, 3, 4, 5, 6, 7]
    # First characters of pages 4–7, as on the scans.
    assert starts[4].startswith("多於魯論")
    assert starts[5].startswith("一篇")
    assert starts[6].startswith("亦爲之訓說")
    assert starts[7].startswith("集諸家")


def test_counted_page_numbers_agree_with_page_list(fx):
    page_nums = {p["pageId"]: p["pageNum"] for p in fx["pages"]}
    counted = [b["page"] for b in _chapter(fx)["page_starts"]]
    mapped = [b["page"] for b in _chapter(fx, page_nums)["page_starts"]]
    assert counted == mapped


def test_heading_detected(fx):
    ch = _chapter(fx)
    assert ch["headings"] == [0]
    assert ch["paragraphs"][0] == "論語序"


def test_markdown_and_text_have_page_markers(fx):
    data = ShidianGujiParser().parse(fx["paragraphs"], "SBCK011", "u", {"title": "論語"}).to_dict()
    md = MarkdownConverter().convert(data)
    assert "*〔第3页〕*" in md and "### 論語序" in md
    assert "頗〔第4页〕多於魯論" in md
    txt = PlainTextConverter().convert(data)
    assert "〔第1页〕〔第2页〕〔第3页〕" in txt and "頗〔第4页〕多於魯論" in txt


def test_converters_unchanged_without_page_data():
    data = {"title": "t", "chapters": [{"title": "c", "paragraphs": ["甲", "乙"]}]}
    assert "〔第" not in MarkdownConverter().convert(data)
    assert PlainTextConverter().convert(data) == "c\n\n甲\n乙\n"


def test_page_text_layer_maps_boxes_to_image_files(fx):
    a = ShidianGujiAdapter()
    a._page_nums = {p["pageId"]: int(p["pageNum"]) for p in fx["pages"]}
    a._word_boxes = {pid: v["wordBoxList"] for pid, v in fx["word_boxes"].items()}
    layout = a.page_text_layer({3: "0003.webp", 4: "0004.webp"})
    page = layout["pages"]["0003.webp"]
    assert page["page"] == 3
    assert "".join(c["t"] for c in page["chars"]).startswith("論語序")
    assert len(page["chars"][0]["poly"]) == 8
    assert a.page_text_layer({}) is None


def test_text_layer_is_searchable(fx, tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from PIL import Image
    from bookget.pdf.sheet import find_cjk_font
    from bookget.pdf.text_layer import add_text_layer
    font = find_cjk_font()
    if not font:
        pytest.skip("no CJK font on this machine")

    a = ShidianGujiAdapter()
    a._page_nums = {p["pageId"]: int(p["pageNum"]) for p in fx["pages"]}
    a._word_boxes = {pid: v["wordBoxList"] for pid, v in fx["word_boxes"].items()}
    (tmp_path / "page_layout.json").write_text(
        json.dumps(a.page_text_layer({3: "0003.jpg"}), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "images").mkdir()
    Image.new("RGB", (817, 1287), "white").save(tmp_path / "images" / "0003.jpg")

    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=817 * 72 / 300, height=1287 * 72 / 300)
    page.insert_image(page.rect, filename=str(tmp_path / "images" / "0003.jpg"))
    doc.save(str(pdf))
    doc.close()

    assert add_text_layer(pdf, tmp_path, ["images/0003.jpg"], font) == 1
    doc = pymupdf.open(str(pdf))
    lines = [ln for ln in doc[0].get_text().splitlines() if ln]
    # Each column comes out as one line, so phrases are searchable.
    assert lines[0] == "論語序"
    assert any("漢中壘校尉劉向" in ln for ln in lines)
    assert doc[0].search_for("漢中壘校尉劉向")
    # 論 box is x 687–776, y 204–269 px on an 817×1287 image.
    r = doc[0].search_for("論")[0]
    s = doc[0].rect.width / 817
    assert abs(r.x0 / s - 687) < 6 and abs(r.x1 / s - 776) < 6
    assert abs(r.y0 / s - 204) < 6 and abs(r.y1 / s - 269) < 6
