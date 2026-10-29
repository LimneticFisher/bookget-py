"""TaskManager — wraps ResourceManager for concurrent HTTP-driven downloads."""
import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.resource_manager import ResourceManager
from ..models.book import ResourceType
from ..models.manifest import DownloadManifest, NodeStatus
from ..adapters.base import BaseSiteAdapter
from ..adapters.registry import AdapterRegistry, get_adapter
from ..logger import logger
from .sse import EventBus


@dataclass
class TaskInfo:
    task_id: str
    url: str
    output_dir: str
    status: str = "pending"          # pending | running | completed | failed | cancelled
    asyncio_task: Any = field(default=None, repr=False)
    manifest: DownloadManifest | None = None
    error: str | None = None


class TaskManager:
    """
    Manages active download tasks and exposes methods for the HTTP API.
    All methods are coroutines safe to call from aiohttp handlers.
    """

    def __init__(self, config: Config, bus: EventBus):
        self.config = config
        self.bus = bus
        self._tasks: dict[str, TaskInfo] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    async def search(
        self, site_id: str, query: str, limit: int = 20, offset: int = 0,
    ) -> dict:
        """Search for books on a specific site."""
        manager = ResourceManager(self.config)
        try:
            return await manager.search(site_id, query, limit, offset)
        finally:
            await manager.close()

    async def discover(
        self, url: str, output_dir: str | None, depth: int = 1, index_id: str = "",
        output_root: str | None = None,
    ) -> dict:
        """Discover book structure and return manifest as dict.

        `output_root` is a parent folder: the book gets its own
        `<output_root>/<book_id>` subfolder, as the CLI does. Passing the root
        as `output_dir` instead put every book's manifest.json in the same
        folder, each overwriting the last. The folder actually used comes back
        as "output_dir".
        """
        manager = ResourceManager(self.config)
        try:
            if output_root and not output_dir:
                adapter = get_adapter(url, self.config)
                if adapter:
                    book_id = adapter.extract_book_id(url)
                    output_dir = str(Path(output_root) / manager.storage._sanitize_filename(book_id))
            manifest = await manager.discover(
                url=url,
                output_dir=Path(output_dir) if output_dir else None,
                depth=depth,
                index_id=index_id,
            )
            result = manifest.to_dict()
            if output_dir:
                result["output_dir"] = str(Path(output_dir).resolve())
            return result
        finally:
            await manager.close()

    async def expand_node(
        self, url: str, output_dir: str, node_id: str
    ) -> dict:
        """Expand a manifest node and return updated manifest as dict."""
        manager = ResourceManager(self.config)
        try:
            manifest = await manager.expand_manifest_node(
                url=url,
                output_dir=Path(output_dir),
                node_id=node_id,
            )
            return manifest.to_dict()
        finally:
            await manager.close()

    def start_download(
        self,
        url: str,
        output_dir: str,
        node_ids: list[str] | None = None,
        concurrency: int = 1,
        index_id: str = "",
        task_id: str | None = None,
        make_pdf: bool = False,
    ) -> str:
        """
        Start an incremental download task asynchronously.
        Returns task_id immediately; progress is pushed via EventBus.

        `task_id` lets the caller supply its own id. The web UI keys its
        manifests and progress bars by the id it already used for discovery,
        so minting a fresh server-side id made every SSE event arrive under an
        id the UI had never seen: node statuses never refreshed, and each run
        added another progress bar that could not be matched to the old one.
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        info = TaskInfo(task_id=task_id, url=url, output_dir=output_dir)
        self._tasks[task_id] = info

        info.asyncio_task = asyncio.create_task(
            self._run_download(task_id, url, output_dir, node_ids, concurrency, index_id,
                               make_pdf)
        )
        return task_id

    async def cancel(self, task_id: str) -> bool:
        info = self._tasks.get(task_id)
        if not info:
            return False
        if info.asyncio_task and not info.asyncio_task.done():
            info.asyncio_task.cancel()
            info.status = "cancelled"
            self.bus.publish("task_error", {"taskId": task_id, "message": "已取消"})
            return True
        return False

    async def delete_nodes(
        self, task_id: str, node_ids: list[str], output_dir: str | None = None,
    ) -> dict:
        """Delete a node's downloaded files and reset it to PENDING.

        Two things used to be wrong here:
          * it only flipped the manifest status and **deleted no files at all**,
            yet still answered {"deleted": true} — a plain false success;
          * it required a live in-memory task, so deleting right after
            「发现结构」(no download started yet) just 404'd.

        Now the manifest is loaded from disk when the task isn't in memory, the
        files listed in each node's source_data are actually removed, and the
        caller gets the real count back.
        """
        from ..models.manifest import NodeStatus

        info = self._tasks.get(task_id)
        base_dir = Path(output_dir or (info.output_dir if info else "") or ".")

        manifest = info.manifest if info else None
        if manifest is None:
            # Fall back to the manifest on disk so delete works after a plain
            # discover, or after the server was restarted.
            manifest_path = base_dir / "manifest.json"
            if not manifest_path.is_file():
                return {"deleted": False, "error": "manifest not found",
                        "removed_files": 0}
            try:
                manifest = DownloadManifest.from_dict(
                    json.loads(manifest_path.read_text(encoding="utf-8")))
            except Exception as e:
                return {"deleted": False, "error": f"cannot read manifest: {e}",
                        "removed_files": 0}

        manager = ResourceManager(self.config)
        removed = 0
        missing_nodes: list[str] = []
        try:
            path_map = manager._build_node_path_map(manifest.root, base_dir)
            for node_id in node_ids:
                node = manifest.find_node(node_id)
                if node is None:
                    missing_nodes.append(node_id)
                    continue
                # Deleting a parent must also clear everything beneath it,
                # otherwise a volume's pages survive while it claims PENDING.
                for target in self._walk_nodes(node):
                    target_dir, _ = path_map.get(target.id, (base_dir, None))
                    removed += self._remove_node_files(target, Path(target_dir))
                    # DISCOVERED, not PENDING: get_downloadable_nodes() only
                    # picks up {DISCOVERED, FAILED, DOWNLOADING}, so resetting
                    # to PENDING ("children not yet expanded") left the node
                    # permanently un-redownloadable — the UI would report
                    # "0 downloadable" forever after a delete.
                    target.status = NodeStatus.DISCOVERED
                    target.downloaded_items = 0

            manager._save_hierarchical_manifests(manifest, base_dir)
            if info is not None:
                info.manifest = manifest
            # Tell the UI so the tree repaints without a manual re-discover.
            self.bus.publish("manifest_updated", {
                "taskId": task_id,
                "manifest": manifest.to_dict(),
            })
            return {
                "deleted": True,
                "removed_files": removed,
                "missing_nodes": missing_nodes,
            }
        finally:
            await manager.close()

    @staticmethod
    def _walk_nodes(node):
        """Yield `node` and every descendant, deepest first."""
        for child in (getattr(node, "children", None) or []):
            yield from TaskManager._walk_nodes(child)
        yield node

    @staticmethod
    def _remove_node_files(node, node_dir: Path) -> int:
        """Delete the files a node produced. Returns how many were removed."""
        removed = 0
        source = getattr(node, "source_data", None) or {}
        names = [
            item.get("filename")
            for item in (source.get("images") or [])
            if isinstance(item, dict) and item.get("filename")
        ]
        for name in names:
            target = node_dir / name
            try:
                if target.is_file():
                    target.unlink()
                    removed += 1
            except OSError as e:
                logger.warning(f"could not delete {target}: {e}")

        # Non-leaf nodes own a directory; drop it once it's empty.
        try:
            if (node_dir.is_dir() and node_dir.name
                    and not any(node_dir.iterdir())):
                node_dir.rmdir()
        except OSError:
            pass
        return removed

    def get_task(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[dict]:
        return [
            {
                "task_id": t.task_id,
                "url": t.url,
                "output_dir": t.output_dir,
                "status": t.status,
                "manifest_progress": t.manifest.get_progress() if t.manifest else None,
                "error": t.error,
            }
            for t in self._tasks.values()
        ]

    def get_supported_sites(self) -> list[dict]:
        return AdapterRegistry.list_adapters()

    # Region headings for the GUI's site picker (same grouping as the README).
    _SITE_REGIONS = {
        "中国大陆": ["nlc_guji", "nlc_read", "shidianguji", "ctext"],
        "日本": ["ndl", "kyoto_rmda", "tnm", "kokusho", "keio", "toyobunko",
                "emuseum", "khirin", "ryukoku"],
        "港台": ["ncl_rbook", "npm_taipei", "hanchi", "taiwan_ebook", "hku", "hkust"],
        "欧美图书馆": ["harvard", "princeton", "stanford", "berkeley", "loc", "bodleian",
                    "cambridge_cudl", "vatican", "british_library", "bnf_gallica",
                    "berlin_sbb", "bsb", "onb", "smithsonian"],
        "通用": ["wikimedia_commons", "wikisource", "archive_org", "generic_iiif"],
    }

    def get_search_sites(self) -> list[dict]:
        """Every supported site for the GUI's picker, grouped by region.

        Only sites with supports_search can be searched in-app; for the rest
        the GUI opens `home` so the user can find a book and paste its link.
        """
        AdapterRegistry.list_adapters()  # ensure adapters are discovered
        adapters = AdapterRegistry._adapters
        order = [(r, sid) for r, ids in self._SITE_REGIONS.items() for sid in ids]
        listed = {sid for _, sid in order}
        order += [("其他", sid) for sid in adapters if sid not in listed]
        sites = []
        for region, sid in order:
            cls = adapters.get(sid)
            if not cls:
                continue
            domains = list(cls.site_domains)
            sites.append({
                "site_id": sid,
                "site_name": cls.site_name,
                "region": region,
                "home": f"https://{domains[0]}" if domains else "",
                "supports_search": bool(getattr(cls, "supports_search", False)),
                "supports_match": callable(getattr(cls, "match_book", None)),
                "supports_images": bool(getattr(cls, "supports_images", False)),
                "supports_text": bool(getattr(cls, "supports_text", False)),
                "supports_pdf": bool(getattr(cls, "supports_pdf", False)),
            })
        return sites

    async def match(
        self, site_id: str, title: str, authors: list[str] | None = None,
    ) -> dict:
        """Exact title (+ author) match against one site's catalog."""
        manager = ResourceManager(self.config)
        try:
            return await manager.match_book(site_id, title, authors or [])
        finally:
            await manager.close()

    def check_url(self, url: str) -> dict:
        adapter = AdapterRegistry.get_for_url(url)
        if adapter:
            return {
                "supported": True,
                "site": {
                    "site_id": adapter.site_id,
                    "site_name": adapter.site_name,
                    "site_domains": list(adapter.site_domains),
                    "supports_text": adapter.supports_text,
                    "supports_images": getattr(adapter, "supports_images", True),
                    "supports_pdf": getattr(adapter, "supports_pdf", False),
                },
            }
        return {"supported": False}

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _run_download(
        self,
        task_id: str,
        url: str,
        output_dir: str,
        node_ids: list[str] | None,
        concurrency: int,
        index_id: str,
        make_pdf: bool = False,
    ):
        info = self._tasks[task_id]
        info.status = "running"
        manager = ResourceManager(self.config)

        def progress_cb(completed: int, total: int):
            self.bus.publish("progress", {
                "taskId": task_id,
                "completed": completed,
                "total": total,
                "percent": round(completed * 100 / total) if total > 0 else 0,
            })

        def status_cb(event_type: str, data: dict):
            if event_type not in ("downloading", "downloaded", "expanded"):
                return
            # Prefer the LIVE manifest carried on the event. info.manifest is
            # only assigned after download_incremental() returns, so using it
            # here re-published the stale discovery-time copy and every volume
            # stayed "待下载" in the UI until the user hit 发现结构 again.
            live = data.get("manifest")
            manifest_obj = live if live is not None else info.manifest
            if manifest_obj is None:
                return
            # Keep the task's own copy current too, so /api/tasks reflects
            # in-flight state rather than lagging a whole run behind.
            if live is not None:
                info.manifest = live
            self.bus.publish("manifest_updated", {
                "taskId": task_id,
                "manifest": manifest_obj.to_dict(),
            })

        try:
            if self._has_node_download(url):
                manifest = await manager.download_incremental(
                    url=url,
                    output_dir=Path(output_dir),
                    node_ids=node_ids,
                    index_id=index_id,
                    progress_callback=progress_cb,
                    status_callback=status_cb,
                    concurrency=max(1, concurrency),
                )
            else:
                manifest = await self._download_whole_book(
                    manager, url, output_dir, index_id, progress_cb, task_id)
            info.manifest = manifest
            pdf_path = None
            if make_pdf:
                self.bus.publish("progress", {"taskId": task_id, "phase": "pdf"})
                try:
                    pdf_path = await self.build_pdf(output_dir)
                except Exception as e:
                    raise RuntimeError(f"下载已完成，但生成 PDF 失败：{e}") from e
            info.status = "completed"
            self.bus.publish("task_completed", {
                "taskId": task_id,
                "manifest": manifest.to_dict() if manifest else None,
                "pdfPath": pdf_path,
            })
        except asyncio.CancelledError:
            info.status = "cancelled"
        except Exception as exc:
            info.status = "failed"
            info.error = str(exc)
            self.bus.publish("task_error", {
                "taskId": task_id,
                "message": str(exc),
            })
        finally:
            await manager.close()

    def _has_node_download(self, url: str) -> bool:
        """Whether the site's adapter implements per-node download_node()."""
        adapter = get_adapter(url, self.config)
        return adapter is not None and (
            type(adapter).download_node is not BaseSiteAdapter.download_node)

    async def _download_whole_book(
        self, manager: ResourceManager, url: str, output_dir: str,
        index_id: str, progress_cb, task_id: str = "",
    ) -> DownloadManifest | None:
        """Download with the CLI's whole-book path, then mark the manifest done.

        Adapters such as 识典古籍 only implement get_image_list() /
        get_structured_text(); download_incremental() would call their
        download_node() and fail every node with NotImplementedError. Node
        selection does not apply here: the whole book is fetched.
        """
        dest = Path(output_dir)
        def listing_cb(found: int, total: int):
            self.bus.publish("progress", {
                "taskId": task_id, "phase": "listing", "completed": found, "total": total,
                "percent": round(found * 100 / total) if total else 0,
            })

        task = await manager.download(
            url, output_dir=dest, index_id=index_id, progress_callback=progress_cb,
            listing_callback=listing_cb)
        if task.failed_count:
            raise RuntimeError(
                f"{task.failed_count}/{task.total_resources} 个文件下载失败，可重新下载以补全")
        # A short image list is not a failure to the legacy path (pages it
        # could not sign are skipped), so check against the book's page count.
        adapter = get_adapter(url, self.config)
        expected = (getattr(task.metadata, "pages", 0) or 0) if (
            adapter and getattr(adapter, "supports_images", False)) else 0
        got = sum(1 for r in task.resources if r.resource_type == ResourceType.IMAGE)
        if expected and got < expected:
            # Retrying does not help: the 识典 reader only preloads the pages
            # around where it opens, and bookget can't page through the book.
            text_done = (dest / "text" / "full.md").is_file()
            raise RuntimeError(
                f"图片只获取到 {got}/{expected} 页：该站阅读器只预加载开头几页，"
                f"bookget 目前无法获取整本书的图片，重试也不会改善。"
                + ("全文（带页码）已完整保存在 text/full.md。" if text_done else ""))

        manifest = DownloadManifest.load(dest / ResourceManager.MANIFEST_FILENAME)
        if manifest:
            def mark(node):
                node.status = NodeStatus.COMPLETED
                node.downloaded_items = node.total_items
                for child in node.children:
                    mark(child)
            mark(manifest.root)
            manager._save_hierarchical_manifests(manifest, dest)
        return manifest

    @staticmethod
    def relocate(src: str, dst: str) -> None:
        """Move a book folder to the one the user picked after discovery.

        Files already present at the destination win; the source's copies are
        then left in place rather than deleted. Emptied directories are removed.
        """
        s, d = Path(src).resolve(), Path(dst).resolve()
        if s == d or not s.is_dir():
            return
        d.mkdir(parents=True, exist_ok=True)
        for item in sorted(s.rglob("*"), reverse=True):
            if item.is_file():
                target = d / item.relative_to(s)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(target))
            elif item.is_dir() and not any(item.iterdir()):
                item.rmdir()
        if not any(s.iterdir()):
            s.rmdir()

    @staticmethod
    async def build_pdf(book_dir: str) -> str:
        """Archival PDF for a downloaded book (same as `bookget pdf <dir>`)."""
        from ..pdf import build_book_pdf

        if not (Path(book_dir) / "images").is_dir():
            raise RuntimeError("这本书没有下载图片，无法生成 PDF")
        out = await asyncio.to_thread(build_book_pdf, book_dir)
        return str(out)
