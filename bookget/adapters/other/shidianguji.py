# 识典古籍 (Shidianguji) Adapter
# https://www.shidianguji.com/
#
# Uses Playwright browser automation to bypass ByteDance SecSDK anti-bot protection.
# SecSDK generates device fingerprints (verifyFp / a_bogus) required for all API calls.

import os
import re
import sys
import asyncio
import json
import urllib.parse
from typing import List, Optional, Callable

from ..base import BaseSiteAdapter
from ..registry import AdapterRegistry
from ...models.book import BookMetadata, Resource, ResourceType, Creator
from ...models.search import MatchedResource, SearchResponse, SearchResult
from ...text_parsers.base import StructuredText
from ...text_parsers.shidianguji_parser import ShidianGujiParser
from ...shared import cjk_match
from ...logger import logger
from ...exceptions import MetadataExtractionError, DownloadError

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@AdapterRegistry.register
class ShidianGujiAdapter(BaseSiteAdapter):
    """
    Adapter for 识典古籍 (shidianguji.com).

    Requires Playwright for browser automation (ByteDance SecSDK auth).
    Install: pip install playwright && playwright install chromium

    Supports:
    - Image download: captures signed CDN URLs by navigating the reader
    - Text download: intercepts unencrypted paragraphs API responses

    URL patterns:
    - Book: https://www.shidianguji.com/zh/book/{book_id}
    - Book: https://www.shidianguji.com/book/{book_id}
    """

    site_name = "识典古籍"
    site_id = "shidianguji"
    site_domains = ["shidianguji.com", "www.shidianguji.com"]

    supports_iiif = False
    supports_images = True
    supports_text = True
    supports_search = True

    BASE_URL = "https://www.shidianguji.com"
    _CDN_HOST = "byteimg.com"

    # Match the paragraphs endpoint at ANY version. The site moved v2 → v3 and
    # the hardcoded "…/paragraphs/v2" match silently stopped firing, so text
    # downloads collected 0 paragraphs while still reporting success (issue #1).
    # Staying version-agnostic means the next bump doesn't break us the same way.
    _PARAGRAPHS_API_RE = re.compile(r"/api/ancientlib/read/book/paragraphs/v\d+")

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # How long to wait for the reader's XHRs after the page loads. Was 20s,
    # which failed outright on slow links (e.g. from Taiwan the page itself
    # takes ~30s and the API lands well after; in 2026-10 the reader's first
    # responses took 75–95s).
    _API_WAIT = 120.0

    def __init__(self, config=None):
        super().__init__(config)
        self._pw = None
        self._browser = None
        self._context = None
        # discover() asks for metadata twice (structure + metadata.json);
        # each call is a full browser page load, so remember the answer.
        self._metadata_cache: dict[str, BookMetadata] = {}
        # Page layout the reader loads on its own while we drive it: pageId →
        # pageNum, and pageId → per-character boxes (word_box API). Only what
        # the page itself requests is recorded; nothing is fetched for it.
        self._page_nums: dict[str, int] = {}
        self._word_boxes: dict[str, list] = {}
        # Paragraphs (paragraphId → paragraph) and bookInfo the reader loaded
        # while get_image_list read the book, so the text step can reuse them
        # instead of reading the whole book a second time.
        self._paragraphs: dict[str, dict] = {}
        self._book_info: dict = {}
        self._read_book_id = ""

    def _check_playwright(self):
        if not HAS_PLAYWRIGHT:
            # The packaged .exe deliberately ships without Playwright/Chromium
            # (too large), so exe users land here. Say so in Chinese and point
            # at the fix rather than dumping an English ImportError.
            frozen = getattr(sys, "frozen", False)
            if frozen:
                raise DownloadError(
                    "识典古籍需要浏览器内核，而打包版 exe 未内置 Playwright/Chromium"
                    "（体积原因）。\n"
                    "请改用 pip 安装后再下载识典古籍：\n"
                    '  pip install "bookget[browser]"\n'
                    "  playwright install chromium\n"
                    "其余站点不受影响，exe 可正常使用。"
                )
            raise DownloadError(
                "识典古籍需要 Playwright 浏览器自动化（绕过字节跳动 SecSDK 反爬）。\n"
                "请执行：\n"
                '  pip install "bookget[browser]"\n'
                "  playwright install chromium"
            )

    def extract_book_id(self, url: str) -> str:
        """Extract book ID from URL."""
        match = re.search(r'/book/([a-zA-Z0-9_]+)', url)
        if match:
            return match.group(1)
        raise MetadataExtractionError(f"Could not extract book ID from URL: {url}")

    async def _ensure_browser(self):
        """Ensure a persistent browser instance is available (reuse across calls).

        Runs headless. An older comment here claimed headless=False was
        required because ByteDance SecSDK detects headless browsers, but the
        code has always passed headless=True and metadata/text/image paths
        were all verified working headless (2026-09-19). The real cause of
        "识典失效" was the paragraphs endpoint moving v2 → v3, not headlessness.
        """
        if self._context is not None:
            return
        # Same launcher (and user agent / viewport) as the one-shot calls.
        self._pw, self._browser, self._context = await self._launch_browser()

    async def _close_browser(self):
        """Close the persistent browser instance."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    @staticmethod
    def _proxy_args() -> list:
        """Connect directly unless BOOKGET_SHIDIAN_PROXY=1.

        Chromium follows the Windows system proxy by default. Through a local
        rule-based proxy (seen with Clash on port 7897) the reader's page
        images never loaded, while a direct connection loaded them.
        """
        return [] if os.environ.get("BOOKGET_SHIDIAN_PROXY") == "1" else ["--no-proxy-server"]

    async def _launch_browser(self):
        """Start Playwright, launch Chromium, return (pw, browser, context).

        Used by get_metadata / get_image_list / get_structured_text which
        manage their own browser lifecycle. Always a fresh, empty browser (as
        upstream bookget does): a persistent profile made repeat parses faster
        by caching the site's scripts, but it got flagged as automated and
        every request from it was refused (errorCode 31000).
        """
        self._check_playwright()
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=True, args=self._proxy_args())
        except Exception as e:
            await pw.stop()
            raise DownloadError(
                "Chromium 浏览器未安装。请运行: playwright install chromium"
            ) from e
        context = await browser.new_context(
            user_agent=self._USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        return pw, browser, context

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def _fetch_book_info(self, book_id: str, book_info: dict) -> None:
        """One browser page load; fills book_info from the reader-book API."""
        api_done = asyncio.Event()

        pw, browser, context = await self._launch_browser()
        try:
            page = await context.new_page()

            async def on_response(response):
                if "/api/ancientlib/read/reader-book/get/" in response.url:
                    try:
                        data = await response.json()
                        if data.get("errorCode") == 0:
                            book_info.update(data["data"].get("bookInfo", {}))
                            api_done.set()
                    except Exception:
                        pass

            page.on("response", on_response)
            book_url = f"{self.BASE_URL}/zh/book/{book_id}"
            # Don't wait for the page to finish: the API often lands first.
            goto = asyncio.ensure_future(
                page.goto(book_url, wait_until="domcontentloaded", timeout=90000))
            try:
                await asyncio.wait_for(api_done.wait(), timeout=90.0)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for metadata API for {book_id}")
            if not goto.done():
                goto.cancel()
            try:
                await goto
            except BaseException as e:  # cancelled or navigation error
                if not book_info:
                    logger.warning(f"Page.goto: {e!r}")
        finally:
            await browser.close()
            await pw.stop()

    async def discover_structure(
        self, book_id: str, index_id: str = "", depth: int = 1,
        progress_callback: Callable[[str, str], None] = None,
    ):
        """Metadata-only manifest, so the preview appears in seconds.

        Listing images means scrolling the reader through every page (minutes
        for a few hundred pages), and the URLs are short-lived signed links
        the whole-book download collects again anyway.
        """
        return await self._discover_from_legacy(book_id, index_id, list_images=False)

    # Character boxes: word_box/page_content until 2026-10, now
    # word-box-page-content/m-get (same payload).
    _WORD_BOX_API_RE = re.compile(r"/api/ancientlib/read/word[-_]box")
    _PAGES_API = "/api/ancientlib/read/book/pages/v"
    _BOOK_INFO_API = "/api/ancientlib/read/reader-book/get/"

    async def _record_layout(self, response) -> None:
        """Keep book info, paragraphs, page numbers and character boxes from
        the reader's own responses."""
        url = response.url
        is_paragraphs = bool(self._PARAGRAPHS_API_RE.search(url))
        if not (self._WORD_BOX_API_RE.search(url) or self._PAGES_API in url
                or self._BOOK_INFO_API in url or is_paragraphs):
            return
        try:
            data = await response.json()
        except Exception:
            return
        if data.get("errorCode") != 0:
            return
        payload = data.get("data") or {}
        if self._BOOK_INFO_API in url:
            self._book_info.update(payload.get("bookInfo") or {})
        if is_paragraphs:
            for p in payload.get("paragraphs") or []:
                pid = str(p.get("paragraphId", ""))
                if pid:
                    self._paragraphs.setdefault(pid, p)
        for pg in payload.get("pages", []):
            try:
                self._page_nums[str(pg["pageId"])] = int(pg["pageNum"])
            except (KeyError, TypeError, ValueError):
                pass
        for pid, content in (payload.get("pageId2WordBoxContent") or {}).items():
            boxes = (content or {}).get("wordBoxList") or []
            if boxes:
                self._word_boxes[str(pid)] = boxes

    @staticmethod
    def _catalog_chapters(book_info: dict) -> list[dict]:
        """bookInfo.catalog as a flat chapter list (sub-chapters included).

        The catalog is a JSON string or object, either a list of chapters or
        {"chapters": [...]}; chapterName is a list of {"content": …} runs.
        """
        cat = book_info.get("catalog") or []
        if isinstance(cat, str):
            try:
                cat = json.loads(cat)
            except ValueError:
                cat = []
        if isinstance(cat, dict):
            cat = cat.get("chapters") or []
        out: list[dict] = []

        def walk(items):
            for ch in items or []:
                if not isinstance(ch, dict):
                    continue
                name = ch.get("chapterName")
                if isinstance(name, list):
                    name = "".join(str(x.get("content", "")) for x in name if isinstance(x, dict))
                out.append({**ch, "title": ch.get("title") or name or ""})
                walk(ch.get("subChapters"))
        walk(cat if isinstance(cat, list) else [])
        return out

    def _expected_paragraphs(self) -> int:
        """Paragraphs in the whole book per the catalog (0 when unknown)."""
        return sum(int(ch.get("paragraphCount") or 0) for ch in self._catalog_chapters(self._book_info))

    def _text_complete(self) -> bool:
        expected = self._expected_paragraphs()
        return bool(expected) and len(self._paragraphs) >= expected

    async def _read_through(self, page, pane: str, done: Callable[[], bool],
                            refused: asyncio.Event, report: Callable[[], None] = None) -> None:
        """Scroll one pane of the reader to its end, slowly."""
        stuck = 0
        while stuck < 2 and not refused.is_set():
            moved = await page.evaluate(self._SCROLL_TEXT_JS, [pane, self._SCROLL_STEP])
            await asyncio.sleep(self._SCROLL_PAUSE)
            stuck = 0 if moved else stuck + 1
            if report:
                report()
            if done():
                return

    async def _read_volumes(self, page, done: Callable[[], bool], refused: asyncio.Event,
                            report: Callable[[], None] = None,
                            status: Callable[[], str] = None) -> None:
        """Open each volume from the table of contents and scroll its text to
        the end, slowly, so the reader loads that volume's pages, images,
        paragraphs and character boxes by itself. Stops when done() is true or
        the site starts refusing requests."""

        async def read_through_current_volume() -> None:
            await self._read_through(page, self._TEXT_PANE, done, refused, report)

        await asyncio.sleep(self._CHAPTER_PAUSE)
        await read_through_current_volume()

        # The table of contents is a virtualised list: only the items in
        # view exist in the DOM, so scroll it to reveal the rest.
        toc = page.locator(self._TOC_ITEM)
        texts = await toc.all_inner_texts()
        visited = {texts[0]} if texts else set()
        logger.info("[识典古籍] reading the book volume by volume, slowly")
        while not refused.is_set() and not done():
            texts = await toc.all_inner_texts()
            todo = next((i for i, t in enumerate(texts) if t not in visited), None)
            if todo is None:
                before = texts
                await page.evaluate(self._SCROLL_TEXT_JS, [self._TOC_LIST, 0.8])
                await asyncio.sleep(1.5)
                if await toc.all_inner_texts() == before:
                    break                       # end of the table of contents
                continue
            visited.add(texts[todo])
            try:
                await toc.nth(todo).click(timeout=15000)
            except Exception as e:
                logger.info(f"[识典古籍] could not open 「{texts[todo]}」: {e}")
                continue
            await asyncio.sleep(self._CHAPTER_PAUSE)
            await read_through_current_volume()
            logger.info(f"[识典古籍] 「{texts[todo]}」 ({len(visited)} volumes): "
                        + (status() if status else ""))

    def page_text_layer(self, filenames: dict[int, str]) -> Optional[dict]:
        """Character boxes per downloaded page image, for the PDF text layer.

        filenames maps pageNum → image filename. Coordinates are pixels of
        that image. Returns None when no boxes were seen.
        """
        pages = {}
        for pid, boxes in self._word_boxes.items():
            num = self._page_nums.get(pid)
            name = filenames.get(num) if num is not None else None
            if not name:
                continue
            pages[name] = {
                "page": num,
                "chars": [
                    {"t": b.get("charText", ""), "line": b.get("textlineId", 0),
                     "poly": [b["charPoly"][k] for k in
                              ("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3")]}
                    for b in boxes
                    if b.get("charText") and b.get("charPoly") and not b.get("isPicText")
                ],
            }
        return {"source": self.site_id, "units": "image_px", "pages": pages} if pages else None

    async def get_metadata(self, book_id: str, index_id: str = "") -> BookMetadata:
        """Fetch book metadata by intercepting the reader-book API."""
        if book_id in self._metadata_cache:
            return self._metadata_cache[book_id]
        self._check_playwright()

        book_info: dict = {}
        # The page can be very slow from outside the mainland, and the API
        # sometimes never fires on the first load; one retry recovers most.
        for attempt in range(2):
            await self._fetch_book_info(book_id, book_info)
            if book_info:
                break
            logger.warning(f"[识典古籍] metadata attempt {attempt + 1} failed for {book_id}")

        if not book_info:
            raise MetadataExtractionError(f"Failed to get metadata for {book_id}")

        self._book_info.update(book_info)
        metadata = self._parse_metadata(book_info, book_id)
        self._metadata_cache[book_id] = metadata
        return metadata

    def _parse_metadata(self, info: dict, book_id: str) -> BookMetadata:
        """Convert bookInfo API dict to BookMetadata."""
        metadata = BookMetadata(source_id=book_id)
        metadata.source_url = f"{self.BASE_URL}/zh/book/{book_id}"
        metadata.title = info.get("bookName", "")

        # Dynasty
        metadata.dynasty = info.get("dynastyCategoryName", "")

        # Traditional category hierarchy (cateName field)
        trad_cats = info.get("traditionalCategory", [])
        if trad_cats:
            metadata.category = " / ".join(
                c.get("cateName") or c.get("name", "")
                for c in trad_cats
                if c.get("cateName") or c.get("name")
            )

        # Authors — may be a list or a JSON string
        authors_raw = info.get("authors", [])
        try:
            authors = json.loads(authors_raw) if isinstance(authors_raw, str) else authors_raw
            for a in (authors or []):
                # API uses persName / responsibleTypeStr / dynastyName
                name = a.get("persName") or a.get("name", "")
                role = a.get("responsibleTypeStr") or a.get("role", "")
                dynasty = a.get("dynastyName") or a.get("dynasty", "")
                if name:
                    metadata.creators.append(Creator(name=name, role=role, dynasty=dynasty))
        except (json.JSONDecodeError, TypeError):
            pass

        # Alternative names (may be a list or a comma-separated string)
        add_names = info.get("addNames", "")
        if add_names:
            if isinstance(add_names, list):
                metadata.alt_titles = [n.strip() for n in add_names if n and n.strip()]
            else:
                metadata.alt_titles = [n.strip() for n in str(add_names).split(",") if n.strip()]

        # Total pages
        total_pages = info.get("totalPage", 0)
        if total_pages:
            metadata.pages = total_pages

        # Edition → notes
        edition = info.get("edition", {})
        if edition and edition.get("edition"):
            metadata.notes.append(f"版本: {edition['edition']}")

        # Scanned cover first; the designed "beautiful" cover as fallback.
        metadata.cover_url = info.get("coverUrl") or info.get("beautifulCover") or ""
        if isinstance(info.get("abstract"), str):
            metadata.abstract = info["abstract"].strip()

        metadata.raw_metadata = info
        return metadata

    # ------------------------------------------------------------------
    # Image list
    # ------------------------------------------------------------------

    # Pacing for walking the reader through a whole book. The reader only
    # loads pages near where it is showing, so we move through it the way a
    # person reading would: one volume at a time from the table of contents,
    # scrolling the text a little and pausing. Slow on purpose — reading a
    # whole book this way takes about 3 s a page.
    _SCROLL_STEP = 0.25         # fraction of the text pane per scroll (0.4 skipped ~1 page in 5)
    _SCROLL_PAUSE = 3.0         # seconds between scrolls
    _CHAPTER_PAUSE = 6.0        # seconds after opening a volume
    _TEXT_PANE = ".chapter-reader-container"
    _IMAGE_PANE = ".chapter-reader"
    _TOC_ITEM = ".semi-tree-option"
    _TOC_LIST = ".semi-tree-option-list"

    _SCROLL_TEXT_JS = """([sel, step]) => {
        const e = document.querySelector(sel);
        if (!e) return null;
        const before = e.scrollTop;
        e.scrollTop = before + e.clientHeight * step;
        return e.scrollTop > before;
    }"""

    async def get_image_list(self, book_id: str,
                             progress_callback: Callable[[int, int], None] = None) -> List[Resource]:
        """
        Collect signed CDN image URLs for every page of the book.

        The reader requests a page list and page images only for the volume
        and position it is showing, so a single load yields the first ~10
        pages. This opens each volume from the table of contents in turn and
        scrolls its text slowly, recording the page lists, signed image URLs
        and character boxes the reader loads by itself. Stops at once if the
        site starts refusing requests (errorCode 31000).
        """
        self._check_playwright()

        try:
            expected = int((await self.get_metadata(book_id)).pages or 0)
        except Exception:
            expected = 0

        pages_by_num: dict[int, dict] = {}   # pageNum -> page entry from pages/v3
        image_map: dict[str, str] = {}       # uri -> full signed CDN URL
        pages_event = asyncio.Event()
        refused = asyncio.Event()

        pw, browser, context = await self._launch_browser()
        try:
            page = await context.new_page()

            def _extract_uri_from_cdn_url(url: str) -> Optional[str]:
                """Extract the path URI (e.g. 'read/SBCK001/4/page/…') from a CDN URL."""
                m = re.search(r'/tos-cn-i-[^/]+/(.+?)(?:~|$|\?)', url)
                return m.group(1) if m else None

            async def on_request(request):
                url = request.url
                if self._CDN_HOST in url:
                    logger.debug(f"[识典古籍] image request: {url[:160]}")
                if self._CDN_HOST in url and "ancientlib" in url and ".webp" in url:
                    uri = _extract_uri_from_cdn_url(url)
                    if uri and uri not in image_map:
                        image_map[uri] = url

            async def on_response(response):
                await self._record_layout(response)
                if "/api/ancientlib/" not in response.url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                if data.get("errorCode") == 31000:
                    refused.set()
                if "/api/ancientlib/read/book/pages/v3/" in response.url and data.get("errorCode") == 0:
                    for pg in data["data"]["pages"]:
                        try:
                            pages_by_num[int(pg["pageNum"])] = pg
                        except (KeyError, TypeError, ValueError):
                            pass
                    pages_event.set()

            page.on("request", on_request)
            page.on("response", on_response)

            book_url = f"{self.BASE_URL}/zh/book/{book_id}"
            logger.info(f"[识典古籍] Loading book reader: {book_url}")
            try:
                await page.goto(book_url, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                logger.warning(f"Page.goto: {e}")

            try:
                await asyncio.wait_for(pages_event.wait(), timeout=self._API_WAIT)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for pages API for {book_id}")
            if refused.is_set():
                raise DownloadError("[识典古籍] 站点拒绝了请求（31000，访问过于频繁），请过一段时间再试。")
            if not pages_by_num:
                logger.warning("No page data found; returning empty image list")
                return []

            def captured() -> int:
                return sum(1 for pg in pages_by_num.values() if pg.get("uri") in image_map)

            def report() -> None:
                if progress_callback:
                    try:
                        progress_callback(captured(), expected or len(pages_by_num))
                    except Exception:
                        pass

            # Keep reading until the text is complete too, so the text step
            # can reuse what was loaded here.
            def done() -> bool:
                return bool(expected) and captured() >= expected and self._text_complete()

            def status() -> str:
                return (f"{captured()}/{expected or len(pages_by_num)} page images, "
                        f"{len(pages_by_num)} pages listed, "
                        f"{len(self._paragraphs)}/{self._expected_paragraphs() or '?'} paragraphs, "
                        f"{len(self._word_boxes)} pages with character boxes")

            await self._read_volumes(page, done, refused, report, status)
            if self._paragraphs:
                self._read_book_id = book_id

            if refused.is_set():
                raise DownloadError(
                    f"[识典古籍] 读到一半站点开始拒绝请求（31000），已停止。"
                    f"已获取 {captured()} 页，请过一段时间再试。")
            logger.info(f"[识典古籍] Captured {len(image_map)} signed image URLs "
                        f"for {len(pages_by_num)} listed pages; " + status())

        finally:
            await browser.close()
            await pw.stop()

        return self._resources(pages_by_num, image_map)

    @staticmethod
    def _resources(pages_by_num: dict[int, dict], image_map: dict[str, str],
                   only: Optional[set] = None) -> List[Resource]:
        """Resources for the listed pages whose signed image URL was seen,
        ordered by pageNum (optionally only the page numbers in `only`)."""
        resources: List[Resource] = []
        for num, pg in sorted(pages_by_num.items()):
            if only is not None and num not in only:
                continue
            uri = pg.get("uri", "")
            page_num = pg.get("pageNum", 0)

            # Exact match first, then substring match
            signed_url = image_map.get(uri)
            if not signed_url:
                for img_uri, img_url in image_map.items():
                    if uri and (uri in img_uri or img_uri in uri):
                        signed_url = img_url
                        break

            if signed_url:
                resources.append(Resource(
                    url=signed_url,
                    resource_type=ResourceType.IMAGE,
                    order=page_num,
                    page=str(page_num),
                    filename=f"{page_num:04d}.webp",
                ))
            else:
                logger.warning(f"[识典古籍] No signed URL found for page {page_num} (uri={uri})")

        return resources

    # ------------------------------------------------------------------
    # Chapter by chapter
    # ------------------------------------------------------------------

    def chapter_plan(self) -> list[dict]:
        """Chapters in reading order with the pages each covers, from the
        catalog get_metadata saw: {"id", "title", "start", "end", "paragraphs",
        "content"}. A chapter runs to its endPageNumWithoutSubchapter, else to
        the page the next chapter starts on (a short chapter can share that
        page), the last one to the book's last page."""
        chs = [c for c in self._catalog_chapters(self._book_info) if c.get("chapterId")]
        total = int(self._book_info.get("totalPage") or 0)
        plan = []
        for i, c in enumerate(chs):
            start = int(c.get("startPageNum") or 0)
            nxt = next((int(n.get("startPageNum") or 0) for n in chs[i + 1:]
                        if int(n.get("startPageNum") or 0) >= start), 0)
            end = int(c.get("endPageNumWithoutSubchapter") or 0) or (
                max(start, nxt - 1) if nxt else (total or start))
            paragraphs = int(c.get("paragraphCount") or 0)
            plan.append({
                "id": str(c["chapterId"]), "title": c.get("title") or f"第{i + 1}章",
                "start": start, "end": max(start, end) if start else 0,
                "paragraphs": paragraphs,
                "content": bool(paragraphs or c.get("hasMainContent", True)),
            })
        return plan

    async def read_chapter(self, book_id: str, chapter: dict,
                           progress_callback: Callable[[int, int], None] = None
                           ) -> tuple[List[Resource], list[dict]]:
        """Open the reader at one chapter (a fresh browser, as everywhere
        here) and read that chapter to its end, slowly, recording what the
        reader loads by itself. Returns the chapter's page images and its
        paragraphs; character boxes go to page_text_layer() as usual.
        Raises DownloadError if the site refuses requests (errorCode 31000).
        """
        self._check_playwright()
        cid = chapter["id"]
        lo, hi = chapter.get("start") or 0, chapter.get("end") or 0
        want = set(range(lo, hi + 1)) if lo else set()
        pages_by_num: dict[int, dict] = {}
        image_map: dict[str, str] = {}
        refused = asyncio.Event()
        loaded = asyncio.Event()

        def paragraphs() -> list[dict]:
            return [p for p in self._paragraphs.values() if str(p.get("chapterId", "")) == cid]

        def captured() -> set:
            return {n for n, pg in pages_by_num.items() if n in want and pg.get("uri") in image_map}

        def done() -> bool:
            return want <= captured() and len(paragraphs()) >= chapter.get("paragraphs", 0)

        def report() -> None:
            if progress_callback:
                try:
                    progress_callback(len(captured()), len(want))
                except Exception:
                    pass

        pw, browser, context = await self._launch_browser()
        try:
            page = await context.new_page()

            async def on_request(request):
                url = request.url
                if self._CDN_HOST in url and "ancientlib" in url and ".webp" in url:
                    m = re.search(r'/tos-cn-i-[^/]+/(.+?)(?:~|$|\?)', url)
                    if m and m.group(1) not in image_map:
                        image_map[m.group(1)] = url

            async def on_response(response):
                await self._record_layout(response)
                if "/api/ancientlib/" not in response.url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                if data.get("errorCode") == 31000:
                    refused.set()
                if "/api/ancientlib/read/book/pages/v3/" in response.url and data.get("errorCode") == 0:
                    for pg in data["data"]["pages"]:
                        try:
                            pages_by_num[int(pg["pageNum"])] = pg
                        except (KeyError, TypeError, ValueError):
                            pass
                if pages_by_num or paragraphs():
                    loaded.set()

            page.on("request", on_request)
            page.on("response", on_response)

            url = f"{self.BASE_URL}/book/{book_id}/chapter/{cid}"
            logger.info(f"[识典古籍] 「{chapter.get('title', cid)}」 pages {lo}-{hi}: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                logger.warning(f"Page.goto: {e}")
            try:
                await asyncio.wait_for(loaded.wait(), timeout=self._API_WAIT)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for the reader at chapter {cid}")
            if not refused.is_set() and loaded.is_set():
                await asyncio.sleep(self._CHAPTER_PAUSE)
                await self._read_through(page, self._TEXT_PANE, done, refused, report)
                if not refused.is_set() and not done():
                    # Pages without text (covers, blank leaves) only load when
                    # the image pane itself is scrolled past them.
                    await page.evaluate(
                        "(sel) => { const e = document.querySelector(sel); if (e) e.scrollTop = 0; }",
                        self._IMAGE_PANE)
                    await asyncio.sleep(self._SCROLL_PAUSE)
                    await self._read_through(page, self._IMAGE_PANE, done, refused, report)
            if refused.is_set():
                raise DownloadError("[识典古籍] 站点拒绝了请求（31000，访问过于频繁），已停止；"
                                    "已完成的章节都已保存，过一段时间再点下载会接着下。")
        finally:
            await browser.close()
            await pw.stop()

        got = captured()
        logger.info(f"[识典古籍] 「{chapter.get('title', cid)}」: {len(got)}/{len(want)} page images, "
                    f"{len(paragraphs())}/{chapter.get('paragraphs', 0)} paragraphs")
        paras = sorted(paragraphs(), key=lambda p: (p.get("chapterOrder", 0), p.get("inChapterOrder", 0)))
        return self._resources(pages_by_num, image_map, want), paras

    # ------------------------------------------------------------------
    # Structured text
    # ------------------------------------------------------------------

    async def get_structured_text(
        self,
        book_id: str,
        index_id: str = "",
        progress_callback: Callable[[int, int], None] = None,
    ) -> Optional[StructuredText]:
        """
        Collect paragraph text for all chapters.

        Reuses the paragraphs the reader loaded while get_image_list read the
        book. Otherwise opens the reader and reads it volume by volume the
        same way, recording the paragraphs API responses (v3 at time of
        writing; matched version-agnostically). Text is unencrypted
        (contentEncryptType: 0).
        """
        self._check_playwright()

        if self._read_book_id != book_id or not self._text_complete():
            await self._read_text(book_id, progress_callback)

        paragraphs = list(self._paragraphs.values())
        expected = self._expected_paragraphs()
        logger.info(f"[识典古籍] Collected {len(paragraphs)}/{expected or '?'} paragraphs "
                    f"from {len({str(p.get('chapterId', '')) for p in paragraphs})} chapters")

        # Collecting nothing means the interception never fired (e.g. the
        # site moved the paragraphs endpoint again). Previously this path
        # returned an empty result that the caller reported as a successful
        # download of 0 files — the user-visible shape of issue #1. Fail
        # loudly instead of pretending it worked.
        if not paragraphs:
            raise DownloadError(
                "[识典古籍] 未能抓取到任何段落。可能是站点接口又变了"
                f"（当前匹配 {self._PARAGRAPHS_API_RE.pattern}），"
                "或该书需要登录。请提 issue 附上书籍 URL。"
            )
        if expected and len(paragraphs) < expected:
            logger.warning(f"[识典古籍] 正文只获取到 {len(paragraphs)}/{expected} 段，"
                           "文字不完整（可稍后重试）")

        book_info = self._book_info
        parser = ShidianGujiParser()
        url = f"{self.BASE_URL}/zh/book/{book_id}"
        meta = {
            "title": book_info.get("bookName", ""),
            "authors_json": book_info.get("authors", "[]"),
            "dynasty": book_info.get("dynastyCategoryName", ""),
            "catalog": self._catalog_chapters(book_info),
            "page_nums": dict(self._page_nums),
        }
        return parser.parse(paragraphs, book_id, url, meta)

    async def _read_text(self, book_id: str,
                         progress_callback: Callable[[int, int], None] = None) -> None:
        """Open the reader and read it volume by volume until every paragraph
        in the catalog has loaded. Stops at once on errorCode 31000."""
        refused = asyncio.Event()
        first_paragraphs = asyncio.Event()

        pw, browser, context = await self._launch_browser()
        try:
            page = await context.new_page()

            async def on_response(response):
                await self._record_layout(response)
                if "/api/ancientlib/" not in response.url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                if data.get("errorCode") == 31000:
                    refused.set()
                if self._paragraphs:
                    first_paragraphs.set()

            page.on("response", on_response)

            book_url = f"{self.BASE_URL}/zh/book/{book_id}"
            logger.info(f"[识典古籍] Loading book for text extraction: {book_url}")
            try:
                await page.goto(book_url, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                logger.warning(f"Page.goto: {e}")
            try:
                await asyncio.wait_for(first_paragraphs.wait(), timeout=self._API_WAIT)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for paragraphs API for {book_id}")
            if refused.is_set():
                raise DownloadError("[识典古籍] 站点拒绝了请求（31000，访问过于频繁），请过一段时间再试。")
            if not self._paragraphs:
                return

            def report() -> None:
                if progress_callback:
                    try:
                        progress_callback(len(self._paragraphs), self._expected_paragraphs() or len(self._paragraphs))
                    except Exception:
                        pass

            def status() -> str:
                return f"{len(self._paragraphs)}/{self._expected_paragraphs() or '?'} paragraphs"

            await self._read_volumes(page, self._text_complete, refused, report, status)
            self._read_book_id = book_id
            if refused.is_set():
                logger.warning("[识典古籍] 读到一半站点开始拒绝请求（31000），已停止。")
        finally:
            await browser.close()
            await pw.stop()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResponse:
        """Search 识典古籍 for books matching *query*.

        Navigates to the search page in Playwright and intercepts the
        ``/api/ancientlib/read/search/book/v1`` response.
        """
        all_books = await self._search_books(query)

        total_hits = len(all_books)
        page_results = all_books[offset:offset + limit]
        has_more = (offset + limit) < total_hits

        results = []
        for b in page_results:
            book_id = b.get("bookId", "")
            book_name = b.get("bookName", "")
            authors = b.get("authors", [])
            dynasty = b.get("dynastyCategoryName", "")
            author_str = self._format_authors(authors)
            snippet = b.get("_snippet") or (
                f"（{dynasty}）{author_str}" if dynasty else author_str)
            results.append(SearchResult(
                title=book_name,
                url=f"{self.BASE_URL}/book/{book_id}" if book_id else "",
                snippet=snippet,
                source_site=self.site_id,
                cover_url=b.get("coverUrl") or "",
            ))

        return SearchResponse(
            query=query,
            results=results,
            total_hits=total_hits,
            has_more=has_more,
            continuation=str(offset + limit) if has_more else "",
        )

    async def _search_books(self, query: str) -> list[dict]:
        """Execute a search via Playwright and return the raw book list.

        Strategy:
        1. Navigate to /zh/search/{query} (SSR page with full-text results)
        2. Click the "搜书籍" tab to trigger book search API
        3. Intercept /api/ancientlib/read/search/book/v1 response
        4. Return list of book dicts from searchBookList

        Returns list of dicts with keys: bookId, bookName, authors,
        dynastyCategoryName, addNames, etc.  Each entry is the "book"
        sub-dict unwrapped from the API response.
        """
        await self._ensure_browser()

        search_results: list[dict] = []
        api_done = asyncio.Event()

        page = await self._context.new_page()
        try:
            async def on_response(response):
                if "/api/ancientlib/read/search/book/v1" in response.url:
                    try:
                        data = await response.json()
                        if data.get("errorCode") == 0:
                            book_list = data.get("data", {}).get(
                                "searchBookList", []
                            )
                            for item in book_list:
                                book = item.get("book", item)
                                search_results.append(book)
                        else:
                            # e.g. 31000: the endpoint now rejects automated
                            # clients; fall back to the page's 相关书籍 below.
                            logger.info(f"[识典古籍] book search API errorCode "
                                        f"{data.get('errorCode')}")
                    except Exception:
                        pass
                    api_done.set()

            page.on("response", on_response)

            encoded = urllib.parse.quote(query)
            search_url = f"{self.BASE_URL}/zh/search/{encoded}"
            logger.debug(f"[识典古籍] Searching: {search_url}")
            try:
                await page.goto(
                    search_url, wait_until="domcontentloaded", timeout=90000
                )
            except Exception as e:
                logger.warning(f"[识典古籍] Search page.goto: {e}")

            # The click below waits for the tab to render; the site's scripts
            # can take a minute to arrive from outside the mainland.
            # Click the "搜书籍" tab to trigger the book search API
            try:
                book_tab = page.locator(
                    'div.semi-tabs-tab:has-text("搜书籍")'
                )
                await book_tab.click(timeout=self._API_WAIT * 1000)
            except Exception as e:
                logger.warning(f"[识典古籍] Could not click book tab: {e}")

            try:
                await asyncio.wait_for(api_done.wait(), timeout=self._API_WAIT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[识典古籍] Timeout waiting for book search API "
                    f"for '{query}'"
                )

            if not search_results:
                search_results.extend(await self._related_books(page))

        finally:
            await page.close()

        return search_results

    # The full-text tab (shown first) carries a 「相关书籍」 box rendered into
    # the page, with no API call to intercept.
    _RELATED_BOOKS_JS = """() => [...document.querySelectorAll('a.recommend-item')].map(a => ({
        href: a.getAttribute('href') || '',
        name: (a.querySelector('.recommend-item__name') || {}).innerText || '',
        info: (a.querySelector('.recommend-item__info') || {}).innerText || '',
        cover: (a.querySelector('img') || {}).src || '',
    }))"""

    async def _related_books(self, page) -> list[dict]:
        """Books from the search page's 相关书籍 box, in the API's dict shape."""
        try:
            await page.locator("a.recommend-item").first.wait_for(timeout=15000)
            items = await page.evaluate(self._RELATED_BOOKS_JS)
        except Exception as e:
            logger.info(f"[识典古籍] no 相关书籍 on search page: {e}")
            return []
        books = []
        for it in items:
            m = re.search(r"/book/([A-Za-z0-9_]+)", it.get("href", ""))
            if not m:
                continue
            books.append({
                "bookId": m.group(1),
                "bookName": it.get("name", "").strip(),
                "_snippet": " ".join(it.get("info", "").split()),
                "coverUrl": it.get("cover", ""),
            })
        return books

    @staticmethod
    def _format_authors(authors) -> str:
        """Format authors list/string to a readable string."""
        if isinstance(authors, str):
            try:
                authors = json.loads(authors)
            except (json.JSONDecodeError, TypeError):
                return authors
        if not isinstance(authors, list):
            return ""
        names = []
        for a in authors:
            if isinstance(a, dict):
                names.append(a.get("persName") or a.get("name", ""))
            elif isinstance(a, str):
                names.append(a)
        return "、".join(n for n in names if n)

    @staticmethod
    def _extract_author_names(authors) -> list[str]:
        """Extract author name strings from the API authors field."""
        if isinstance(authors, str):
            try:
                authors = json.loads(authors)
            except (json.JSONDecodeError, TypeError):
                return [authors] if authors else []
        if not isinstance(authors, list):
            return []
        names = []
        for a in authors:
            if isinstance(a, dict):
                name = a.get("persName") or a.get("name", "")
                if name:
                    names.append(name)
            elif isinstance(a, str) and a:
                names.append(a)
        return names

    # ------------------------------------------------------------------
    # match_book — exact title + author matching for book index
    # ------------------------------------------------------------------

    async def match_book(
        self,
        title: str,
        authors: list[str] | None = None,
        delay: float = 1.0,
    ) -> list[MatchedResource]:
        """Match a book by title + author against 识典古籍.

        Strategy:
        1. Generate title variants (simplified/traditional)
        2. Search each variant via Playwright
        3. Filter by exact title match
        4. Verify author/dynasty if authors provided
        5. Return matched resources

        Args:
            title: Book title (e.g. "周易")
            authors: Author names for filtering (e.g. ["王弼"])
            delay: Seconds between search requests

        Returns:
            List of matched resources.
        """
        authors = authors or []
        found: list[MatchedResource] = []
        seen_ids: set[str] = set()

        def add_result(book_id: str, book_name: str, details: str = "",
                       quality: dict | None = None):
            if book_id in seen_ids:
                return
            seen_ids.add(book_id)
            found.append(MatchedResource(
                id="shidianguji",
                name="识典古籍",
                url=f"{self.BASE_URL}/book/{book_id}",
                details=details,
                quality=quality or {},
            ))

        # Generate title variants
        title_variants = cjk_match.generate_title_variants(title)
        search_queries = list(dict.fromkeys(title_variants))[:3]

        # Search with each variant
        all_books: list[dict] = []
        seen_book_ids: set[str] = set()

        for i, query in enumerate(search_queries):
            if i > 0:
                await asyncio.sleep(delay)
            books = await self._search_books(query)
            for b in books:
                bid = b.get("bookId", "")
                if bid and bid not in seen_book_ids:
                    seen_book_ids.add(bid)
                    all_books.append(b)
            # If first query has exact title matches, skip variants
            if books and any(
                cjk_match.title_matches(b.get("bookName", ""), title_variants)
                for b in books
            ):
                break

        # Filter by exact title match
        candidates = [
            b for b in all_books
            if cjk_match.title_matches(b.get("bookName", ""), title_variants)
        ]

        if not candidates:
            return found

        def _extract_details_and_quality(b: dict) -> tuple[str, dict]:
            # details: 不再放朝代/作者（与 Book/Work 主体重复）
            extra = b.get("extra", {})
            # checkType: 1=粗校, 2=精校, 3=AI整理
            check_type = b.get("checkType", 0)
            check_label = {1: "粗校", 2: "精校", 3: "AI整理"}.get(check_type, "")
            quality = {
                "version": b.get("version", 0),
                "total_page": b.get("totalPage", 0),
                "paragraph_count": extra.get("paragraphNumber", 0),
                "has_translation": extra.get("translateStatus", 0) >= 2,
                "edition": b.get("edition", {}).get("edition", ""),
                "check_type": check_label,
            }
            return "", quality

        # No authors to filter — return all title-matched candidates
        if not authors:
            for b in candidates:
                book_id = b.get("bookId", "")
                book_name = b.get("bookName", "")
                details, quality = _extract_details_and_quality(b)
                add_result(book_id, book_name, details, quality)
            return found

        # With authors: classify candidates by match quality
        author_matched: list[dict] = []
        surname_matched: list[dict] = []
        unmatched: list[dict] = []

        for b in candidates:
            result_authors = self._extract_author_names(b.get("authors", []))
            if not result_authors:
                author_matched.append(b)
            elif cjk_match.author_matches(result_authors, authors):
                author_matched.append(b)
            elif cjk_match.surname_matches(result_authors, authors):
                surname_matched.append(b)
            else:
                unmatched.append(b)

        # Use strict matches if any; else surname; else accept all if ≤ 3
        if author_matched:
            accepted = author_matched
        elif surname_matched:
            accepted = surname_matched
        elif len(unmatched) <= 3:
            accepted = unmatched
        else:
            accepted = []

        for b in accepted:
            book_id = b.get("bookId", "")
            book_name = b.get("bookName", "")
            details, quality = _extract_details_and_quality(b)
            add_result(book_id, book_name, details, quality)

        return found

    # ------------------------------------------------------------------

    async def close(self):
        """Clean up persistent browser if any."""
        await self._close_browser()
