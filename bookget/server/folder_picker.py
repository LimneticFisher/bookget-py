"""Native "choose folder" dialog for the web UI.

A page in the browser cannot learn a real filesystem path from <input
type=file>, so the server (running on the user's own machine) shows the OS
dialog and returns the chosen path.
"""
import asyncio
import os
import sys

# FolderBrowserDialog with a TopMost owner so it is not hidden behind the
# browser window. The initial folder comes in via env to avoid quoting.
_WIN_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = '选择古籍保存位置'
$d.ShowNewFolderButton = $true
if ($env:BOOKGET_INITIAL_DIR -and (Test-Path $env:BOOKGET_INITIAL_DIR)) { $d.SelectedPath = $env:BOOKGET_INITIAL_DIR }
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
"""


def _command() -> list[str] | None:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                "-Command", _WIN_SCRIPT]
    if sys.platform == "darwin":
        return ["osascript", "-e",
                'POSIX path of (choose folder with prompt "选择古籍保存位置")']
    return ["zenity", "--file-selection", "--directory", "--title=选择古籍保存位置"]


_open = asyncio.Lock()


async def pick_folder(initial: str = "") -> str | None:
    """Show the dialog; return the chosen folder, or None if cancelled."""
    if _open.locked():
        raise RuntimeError("文件夹选择窗口已经打开，请先完成或关闭它")
    async with _open:
        return await _run_dialog(initial)


async def _run_dialog(initial: str) -> str | None:
    cmd = _command()
    env = {**os.environ, "BOOKGET_INITIAL_DIR": initial or ""}
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
    # cwd: home, not the app folder — a dialog left open would otherwise hold
    # the exe's directory and block updating or deleting it.
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, cwd=os.path.expanduser("~"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        **kwargs,
    )
    out, _ = await proc.communicate()
    path = out.decode("utf-8", errors="replace").strip()
    return path or None
