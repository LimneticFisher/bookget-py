"""aiohttp application factory for bookget server."""
import logging
from pathlib import Path

from aiohttp import web
from aiohttp.web_middlewares import normalize_path_middleware

from ..config import Config
from .sse import EventBus
from .tasks import TaskManager
from .routes import setup_routes

logger = logging.getLogger(__name__)


def create_app(config: Config, static_dir: Path | None = None) -> web.Application:
    """
    Create and configure the aiohttp application.

    Args:
        config: bookget Config object
        static_dir: path to built React app (dist-app/). If None, serves a
                    simple placeholder page at /.
    """
    bus = EventBus()
    task_manager = TaskManager(config, bus)

    app = web.Application(middlewares=[
        normalize_path_middleware(append_slash=False, remove_slash=True),
        _cors_middleware,
    ])

    app["event_bus"] = bus
    app["task_manager"] = task_manager
    app["config"] = config

    # Register API routes
    setup_routes(app)

    # Static files (React app)
    if static_dir and static_dir.exists():
        from .static import setup_static
        setup_static(app, static_dir)
        logger.info(f"Serving static files from: {static_dir}")
    else:
        # Fallback: simple placeholder at /
        async def _placeholder(request):
            return web.Response(
                content_type="text/html",
                text=(
                    "<h2>bookget server is running</h2>"
                    "<p>API available at <a href='/api/sites'>/api/sites</a></p>"
                    "<p>Frontend not built. Run <code>cd ui && npm run build:app</code></p>"
                ),
            )
        app.router.add_get("/", _placeholder)

    return app


async def run_server(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    static_dir: Path | None = None,
    open_browser: bool = True,
):
    """Start the aiohttp server."""
    app = create_app(config, static_dir)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    url = f"http://{host}:{port}"
    logger.info(f"bookget server running at {url}")

    if open_browser:
        import webbrowser
        webbrowser.open(url)

    return runner, url


# Endpoints that touch the local machine beyond bookget's own downloads (move
# folders, open dialogs / Explorer). CORS here allows any origin, so without
# this any website open in the user's browser could call them.
_LOCAL_ORIGIN_ONLY = {"/api/move-book", "/api/pick-folder", "/api/open", "/api/pdf"}


def _is_local_origin(origin: str) -> bool:
    from urllib.parse import urlparse
    return urlparse(origin).hostname in ("localhost", "127.0.0.1", "::1")


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    """Add CORS headers for local dev (allow all origins)."""
    origin = request.headers.get("Origin")
    if (request.path in _LOCAL_ORIGIN_ONLY and origin
            and not _is_local_origin(origin)):
        return web.json_response({"error": "forbidden origin"}, status=403)
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response
