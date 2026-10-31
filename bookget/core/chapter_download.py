"""Chapter-by-chapter download for readers that load one chapter at a time
(识典古籍): each chapter is read in its own short browser session, saved to
its own folder with a PDF of its own, and recorded in chapters.json so a later
run — after quitting the app, or after the site starts refusing requests —
picks up at the first chapter not yet done.

Layout::

    <book_dir>/
      metadata.json
      chapters.json                    progress, one entry per chapter
      chapters/001_卷首/
        metadata.json  images/  text/  page_layout.json  pdf/<…>.pdf
      text/full.md, full.txt           the finished chapters, in order
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..logger import logger
from ..models.book import DownloadTask

PROGRESS_FILE = "chapters.json"
CHAPTERS_DIR = "chapters"
# Pause between chapters: each one is a fresh browser that reads the whole
# chapter, so spacing them out keeps the load on the site low.
CHAPTER_GAP = 20.0
# A chapter that keeps coming back incomplete is not retried forever.
MAX_ATTEMPTS = 3


def load_progress(book_dir: Path) -> dict:
    try:
        return json.loads((Path(book_dir) / PROGRESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_progress(book_dir: Path, state: dict) -> None:
    path = Path(book_dir) / PROGRESS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def progress_summary(book_dir: Path) -> Optional[dict]:
    """{"done", "total", "partial"} from chapters.json, or None if absent."""
    state = load_progress(book_dir)
    chapters = state.get("chapters") or []
    if not chapters:
        return None
    return {
        "done": sum(1 for c in chapters if c.get("status") in ("done", "empty")),
        "partial": sum(1 for c in chapters if c.get("status") == "partial"),
        "total": len(chapters),
    }


def _merge_plan(plan: list[dict], state: dict) -> list[dict]:
    """The site's chapter list, carrying over what earlier runs recorded."""
    old = {c["id"]: c for c in state.get("chapters") or [] if c.get("id")}
    merged = []
    for i, ch in enumerate(plan):
        entry = {**ch, "index": i + 1, "status": "pending", "attempts": 0}
        prev = old.get(ch["id"])
        if prev:
            for key in ("status", "attempts", "dir", "pdf", "images", "paragraphs_got", "missing_pages",
                        "unavailable_pages"):
                if key in prev:
                    entry[key] = prev[key]
        merged.append(entry)
    return merged


def _write_book_text(book_dir: Path, chapters: list[dict]) -> None:
    """text/full.md and full.txt for the whole book from the chapters done so far."""
    parts_md, parts_txt = [], []
    for ch in chapters:
        if not ch.get("dir"):
            continue
        text_dir = book_dir / ch["dir"] / "text"
        for name, parts in (("full.md", parts_md), ("full.txt", parts_txt)):
            f = text_dir / name
            if f.is_file():
                parts.append(f.read_text(encoding="utf-8").strip())
    if not parts_md and not parts_txt:
        return
    out = book_dir / "text"
    out.mkdir(exist_ok=True)
    (out / "full.md").write_text("\n\n".join(parts_md) + "\n", encoding="utf-8")
    (out / "full.txt").write_text("\n\n".join(parts_txt) + "\n", encoding="utf-8")


async def download_by_chapter(manager, url: str, book_dir: Path, *, make_pdf: bool = True,
                              on_progress: Callable[[dict], None] = None,
                              gap: float = CHAPTER_GAP) -> dict:
    """Download every chapter not yet done; returns the saved progress state.

    `manager` is a ResourceManager (its image downloader, text and layout
    writers are reused). `on_progress` receives dicts with "chapter",
    "chapters", "title", "pages_found", "pages" and "done" (chapters finished).
    A DownloadError from the adapter (the site refusing requests) propagates
    after the progress so far has been saved.
    """
    from ..adapters.registry import get_adapter
    from ..text_parsers.shidianguji_parser import ShidianGujiParser

    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    adapter = get_adapter(url, manager.config)
    book_id = adapter.extract_book_id(url)
    try:
        metadata = await adapter.get_metadata(book_id)
        metadata.source_url = url
        metadata.source_site = adapter.site_id
        book_md = metadata.to_dict()
        (book_dir / "metadata.json").write_text(
            json.dumps(book_md, ensure_ascii=False, indent=2), encoding="utf-8")

        state = load_progress(book_dir)
        chapters = _merge_plan(adapter.chapter_plan(), state)
        state = {"book_id": book_id, "url": url, "title": metadata.title, "chapters": chapters}
        _save_progress(book_dir, state)

        def finished() -> int:
            return sum(1 for c in chapters if c["status"] in ("done", "empty"))

        def emit(ch: dict, found: int = 0, pages: int = 0) -> None:
            if on_progress:
                try:
                    on_progress({"chapter": ch["index"], "chapters": len(chapters), "title": ch["title"],
                                 "pages_found": found, "pages": pages, "done": finished()})
                except Exception:
                    pass

        todo = [c for c in chapters
                if c["status"] not in ("done", "empty") and c.get("attempts", 0) < MAX_ATTEMPTS]
        logger.info(f"[{adapter.site_name}] {metadata.title}: {len(chapters)} chapters, "
                    f"{finished()} done, {len(todo)} to read")
        for n, ch in enumerate(todo):
            if not ch["content"]:          # a heading over its sub-chapters
                ch["status"] = "empty"
                _save_progress(book_dir, state)
                continue
            if n:
                await asyncio.sleep(gap)
            emit(ch)
            ch["attempts"] = ch.get("attempts", 0) + 1
            _save_progress(book_dir, state)
            resources, paragraphs = await adapter.read_chapter(
                book_id, ch, progress_callback=lambda f, t, ch=ch: emit(ch, f, t))

            rel = f"{CHAPTERS_DIR}/{ch['index']:03d}_{manager.storage._sanitize_filename(ch['title'])[:40]}"
            cdir = book_dir / rel
            cdir.mkdir(parents=True, exist_ok=True)
            ch["dir"] = rel
            ch_md = {**book_md, "title": f"{metadata.title}·{ch['title']}"}
            (cdir / "metadata.json").write_text(json.dumps(ch_md, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

            task = DownloadTask(book_id=book_id, url=url, output_dir=str(cdir))
            task.resources = resources
            task.total_resources = len(resources)
            if resources:
                img_dir = cdir / "images"
                img_dir.mkdir(exist_ok=True)
                await manager._download_images(
                    task, adapter, img_dir, lambda d, t, ch=ch: emit(ch, d, t))
                manager._save_page_layout(task, adapter, cdir)

            if paragraphs:
                structured = ShidianGujiParser().parse(paragraphs, book_id, url, {
                    "title": ch_md["title"],
                    "catalog": [{"chapterId": ch["id"], "title": ch["title"]}],
                    "page_nums": dict(adapter._page_nums),
                })
                text_dir = cdir / "text"
                text_dir.mkdir(exist_ok=True)
                (text_dir / f"raw.{adapter.site_id}.json").write_text(
                    json.dumps(structured.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                manager._save_readable_text(structured.to_dict(), text_dir)

            images = sorted((cdir / "images").glob("*")) if (cdir / "images").is_dir() else []
            have = {int(p.stem) for p in images if p.stem.isdigit()}
            want = set(range(ch["start"], ch["end"] + 1)) if ch["start"] else set()
            ch["images"] = len(have & want) if want else len(have)
            ch["paragraphs_got"] = len(paragraphs)
            missing = sorted(want - have)
            previously = ch.get("missing_pages")
            # The reader opens a chapter at its first line of text and never
            # shows the leaves before it (covers, blank pages), so a run of
            # missing pages at the start is not worth another read. Neither
            # is a gap that came back exactly the same on a second read.
            leading = (bool(missing) and len(missing) <= 3 and len(missing) * 2 < len(want)
                       and missing == list(range(ch["start"], ch["start"] + len(missing))))
            if leading or (missing and missing == previously):
                ch["unavailable_pages"], missing = missing, []
            ch["missing_pages"] = missing
            complete = not missing and len(paragraphs) >= ch["paragraphs"]

            ch.pop("pdf", None)
            if make_pdf and images:
                from ..pdf import build_book_pdf
                try:
                    pdf = await asyncio.to_thread(build_book_pdf, cdir)
                    ch["pdf"] = str(Path(pdf).relative_to(book_dir))
                except Exception as e:
                    logger.warning(f"[{adapter.site_name}] PDF for 「{ch['title']}」 failed: {e}")
                    complete = False
            ch["status"] = "done" if complete else "partial"
            ch["updated"] = datetime.now(timezone.utc).isoformat()
            _save_progress(book_dir, state)
            _write_book_text(book_dir, chapters)
            emit(ch, ch["images"], len(want))
        return state
    finally:
        await adapter.close()
