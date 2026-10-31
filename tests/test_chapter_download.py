"""Chapter-by-chapter download: chapter page ranges and resuming (offline)."""
import asyncio
import json
from pathlib import Path

import pytest

from bookget.adapters.other.shidianguji import ShidianGujiAdapter
from bookget.core import chapter_download as cd
from bookget.exceptions import DownloadError
from bookget.models.book import BookMetadata, Resource, ResourceType
from bookget.storage.file_storage import FileStorage

CATALOG = {"chapters": [
    {"chapterId": "a", "chapterName": [{"content": "卷首"}], "startPageNum": 1, "paragraphCount": 2,
     "endPageNumWithoutSubchapter": 3},
    {"chapterId": "b", "chapterName": [{"content": "卷一"}], "startPageNum": 4, "paragraphCount": 1},
    {"chapterId": "c", "chapterName": [{"content": "卷二"}], "startPageNum": 6, "paragraphCount": 0,
     "hasMainContent": False,
     "subChapters": [{"chapterId": "c1", "chapterName": [{"content": "上"}], "startPageNum": 6,
                      "paragraphCount": 1}]},
]}


def _adapter():
    a = ShidianGujiAdapter()
    a._book_info = {"bookName": "書", "totalPage": 8, "catalog": json.dumps(CATALOG)}
    return a


def test_chapter_plan_ranges():
    plan = _adapter().chapter_plan()
    assert [(c["id"], c["title"], c["start"], c["end"]) for c in plan] == [
        ("a", "卷首", 1, 3), ("b", "卷一", 4, 5), ("c", "卷二", 6, 6), ("c1", "上", 6, 8)]
    assert plan[0]["paragraphs"] == 2 and plan[2]["paragraphs"] == 0 and not plan[2]["content"]


class FakeAdapter:
    site_id = "shidianguji"
    site_name = "识典古籍"

    def __init__(self, fail_on=None):
        self.read = []
        self.fail_on = fail_on
        self._page_nums = {}
        self._base = _adapter()

    def extract_book_id(self, url):
        return "B1"

    async def get_metadata(self, book_id):
        return BookMetadata(title="書", source_id=book_id, pages=8)

    def chapter_plan(self):
        return self._base.chapter_plan()

    async def read_chapter(self, book_id, ch, progress_callback=None):
        if ch["id"] == self.fail_on:
            raise DownloadError("31000")
        self.read.append(ch["id"])
        res = [Resource(url=f"u{n}", resource_type=ResourceType.IMAGE, order=n, page=str(n),
                        filename=f"{n:04d}.webp") for n in range(ch["start"], ch["end"] + 1)]
        paras = [{"paragraphId": f"{ch['id']}{i}", "chapterId": ch["id"], "inChapterOrder": i,
                  "content": {"lines": [{"lineType": 1, "content": f"{ch['title']}文{i}"}]}}
                 for i in range(ch["paragraphs"])]
        return res, paras

    async def close(self):
        pass


class FakeManager:
    config = None

    def __init__(self):
        self.storage = FileStorage.__new__(FileStorage)

    async def _download_images(self, task, adapter, img_dir, cb=None):
        for r in task.resources:
            (img_dir / r.get_filename()).write_bytes(b"x")

    def _save_page_layout(self, task, adapter, dest):
        pass

    @staticmethod
    def _save_readable_text(data, text_dir):
        from bookget.core.resource_manager import ResourceManager
        ResourceManager._save_readable_text(data, text_dir)


def _run(monkeypatch, tmp_path, adapter):
    monkeypatch.setattr("bookget.adapters.registry.get_adapter", lambda url, cfg=None: adapter)
    return asyncio.run(cd.download_by_chapter(FakeManager(), "https://www.shidianguji.com/book/B1",
                                              tmp_path, make_pdf=False, gap=0))


def test_stops_on_refusal_and_resumes(monkeypatch, tmp_path):
    first = FakeAdapter(fail_on="b")
    with pytest.raises(DownloadError):
        _run(monkeypatch, tmp_path, first)
    assert first.read == ["a"]
    state = cd.load_progress(tmp_path)
    assert [c["status"] for c in state["chapters"]] == ["done", "pending", "pending", "pending"]
    assert cd.progress_summary(tmp_path) == {"done": 1, "partial": 0, "total": 4}
    assert sorted(p.name for p in (tmp_path / "chapters" / "001_卷首" / "images").iterdir()) == [
        "0001.webp", "0002.webp", "0003.webp"]

    # Quit and come back: only the chapters not done are read.
    second = FakeAdapter()
    state = _run(monkeypatch, tmp_path, second)
    assert second.read == ["b", "c1"]           # 卷二 is only a heading over 上
    assert [c["status"] for c in state["chapters"]] == ["done", "done", "empty", "done"]
    md = (tmp_path / "text" / "full.md").read_text(encoding="utf-8")
    assert md.index("卷首文0") < md.index("卷一文0") < md.index("上文0")


def test_incomplete_chapter_is_retried(monkeypatch, tmp_path):
    class Short(FakeAdapter):
        async def read_chapter(self, book_id, ch, progress_callback=None):
            res, paras = await super().read_chapter(book_id, ch, progress_callback)
            return (res[:-1], paras) if ch["id"] == "b" else (res, paras)

    state = _run(monkeypatch, tmp_path, Short())
    b = state["chapters"][1]
    assert b["status"] == "partial" and b["missing_pages"] == [5]
    again = FakeAdapter()
    state = _run(monkeypatch, tmp_path, again)
    assert again.read == ["b"] and state["chapters"][1]["status"] == "done"


def test_leading_pages_the_reader_never_shows_are_not_retried(monkeypatch, tmp_path):
    class NoCover(FakeAdapter):
        async def read_chapter(self, book_id, ch, progress_callback=None):
            res, paras = await super().read_chapter(book_id, ch, progress_callback)
            return (res[1:], paras) if ch["id"] == "a" else (res, paras)

    a = _run(monkeypatch, tmp_path, NoCover())["chapters"][0]
    assert a["status"] == "done" and a["unavailable_pages"] == [1] and a["missing_pages"] == []
