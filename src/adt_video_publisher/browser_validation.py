"""Dependency-free headless-browser contract tests for ADT compatibility assets."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

from .errors import PublishFailedError

VIEWPORTS: Final = ((320, 640), (390, 844), (767, 900), (1024, 768), (1440, 900))
RESULT_MARKER: Final = 'data-high2min-browser-result="'


@dataclass(frozen=True, slots=True)
class BrowserViewportResult:
    width: int
    height: int
    checks: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


@dataclass(frozen=True, slots=True)
class BrowserValidationResult:
    browser: Path
    viewports: tuple[BrowserViewportResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.viewports)

    def to_dict(self) -> dict[str, object]:
        return {
            "browser": str(self.browser),
            "passed": self.passed,
            "viewports": [
                {**asdict(item), "passed": item.passed}
                for item in self.viewports
            ],
        }


def find_chromium(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("HIGH2MIN_BROWSER"):
        candidates.append(os.environ["HIGH2MIN_BROWSER"])
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    if sys.platform == "win32":
        candidates.extend((
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        ))
    elif sys.platform == "darwin":
        candidates.extend((
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ))
    for candidate in candidates:
        path = Path(candidate).expanduser().resolve(strict=False)
        if path.is_file():
            return path
    return None


def _harness_html() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>High2Min ADT browser contract</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden}
#toolbar{position:fixed;left:0;right:0;bottom:0;height:56px;display:flex;flex-wrap:nowrap;gap:6px;align-items:center;justify-content:center;background:#222}
#toolbar button{flex:0 1 64px;min-width:0;height:42px}
#player{position:fixed;right:8px;top:8px;width:240px;background:#111;color:white}
#player video{height:150px;background:#444}
#handle{height:44px;display:flex;align-items:center;justify-content:center}
</style>
<link rel="stylesheet" href="./sign-language-video.css">
<script>
HTMLMediaElement.prototype.play=function(){this.__playing=true;this.dispatchEvent(new Event("play",{bubbles:true}));return Promise.resolve()};
HTMLMediaElement.prototype.pause=function(){this.__pauseCalls=(this.__pauseCalls||0)+1;this.__playing=false;this.dispatchEvent(new Event("pause",{bubbles:true}))};
</script>
<script src="./media-playback-independence.js"></script>
</head><body>
<audio id="narration" src="./content/i18n/en-GB/audio/page_1.mp3"></audio>
<div id="player"><button id="handle" role="button" aria-label="Drag sign language video">Drag</button><button id="close">Close</button><video id="sign" src="./content/i18n/en-GB/video/page_1.mp4"></video></div>
<div id="toolbar"><button>Back</button><button>Contents</button><button>Tools</button><button>Next</button></div>
<script src="./sign-language-video.js"></script>
<pre id="result">pending</pre>
<script>
window.addEventListener("error",function(event){document.body.setAttribute("data-high2min-browser-error",String(event.message||event.error))});
window.addEventListener("unhandledrejection",function(event){document.body.setAttribute("data-high2min-browser-error",String(event.reason))});
(async function(){
  await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(resolve)})});
  var checks={};
  var player=document.getElementById("player"),handle=document.getElementById("handle"),video=document.getElementById("sign"),audio=document.getElementById("narration"),close=document.getElementById("close"),toolbar=document.getElementById("toolbar");
  handle.setPointerCapture=function(){};
  checks.handControlVisible=getComputedStyle(handle).display!=="none"&&handle.getBoundingClientRect().height>=44;
  checks.videoVisible=getComputedStyle(video).display!=="none"&&video.getBoundingClientRect().width>0;
  checks.playerEnhanced=player.hasAttribute("data-sign-language-player")&&handle.hasAttribute("data-sign-language-drag-handle");
  var before=player.getBoundingClientRect();
  handle.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true,pointerId:1,pointerType:"touch",clientX:before.left+10,clientY:before.top+10}));
  handle.dispatchEvent(new PointerEvent("pointermove",{bubbles:true,pointerId:1,pointerType:"touch",clientX:30,clientY:220}));
  handle.dispatchEvent(new PointerEvent("pointerup",{bubbles:true,pointerId:1,pointerType:"touch",clientX:30,clientY:220}));
  var after=player.getBoundingClientRect();
  checks.touchDragMoved=Math.abs(after.top-before.top)>20;
  checks.playerInsideViewport=after.left>=0&&after.top>=0&&after.right<=innerWidth+1&&after.bottom<=innerHeight+1;
  audio.__pauseCalls=0;video.__pauseCalls=0;
  window.addEventListener("play",function(event){if(event.target===video)audio.pause()});
  await video.play();await audio.play();video.pause();
  checks.narrationDetected=document.documentElement.getAttribute("data-read-aloud-playback")==="playing";
  checks.signVideoDetected=video.hasAttribute("data-independent-media-playback");
  checks.audioDoesNotPauseVideo=(video.__pauseCalls||0)===0;
  checks.videoDoesNotPauseAudio=(audio.__pauseCalls||0)===0;
  close.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true,pointerId:2,pointerType:"touch"}));video.pause();
  checks.explicitVideoCloseWorks=(video.__pauseCalls||0)===1;
  var buttons=Array.from(toolbar.querySelectorAll("button")),tops=buttons.map(function(button){return Math.round(button.getBoundingClientRect().top)}),toolbarRect=toolbar.getBoundingClientRect();
  checks.toolbarSingleRow=new Set(tops).size===1;
  checks.toolbarFitsViewport=toolbarRect.left>=0&&toolbarRect.right<=innerWidth+1&&buttons.every(function(button){var rect=button.getBoundingClientRect();return rect.left>=0&&rect.right<=innerWidth+1});
  document.getElementById("result").textContent=JSON.stringify(checks);
  document.body.setAttribute("data-high2min-browser-result",JSON.stringify(checks));
})();
</script></body></html>"""


def _stop_browser_process(process: subprocess.Popen[bytes]) -> None:
    """Stop Chrome and its descendants without leaving a headless process behind."""

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass


def _decoded_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run_browser_command(command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """Capture a complete DOM even when macOS Chrome does not exit after ``--dump-dom``."""

    process_options: dict[str, object] = {}
    if os.name == "posix":
        process_options["start_new_session"] = True
    elif sys.platform == "win32":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **process_options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial_stdout = _decoded_output(exc.output)
        partial_stderr = _decoded_output(exc.stderr)
        _stop_browser_process(process)
        try:
            final_stdout, final_stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            final_stdout, final_stderr = b"", b""
        output = _decoded_output(final_stdout) or partial_stdout
        error_output = _decoded_output(final_stderr) or partial_stderr
        if RESULT_MARKER in output:
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=error_output)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=output,
            stderr=error_output,
        ) from exc
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=_decoded_output(stdout),
        stderr=_decoded_output(stderr),
    )


def run_browser_contract_tests(
    browser_path: str | os.PathLike[str] | None = None,
    *,
    viewports: tuple[tuple[int, int], ...] = VIEWPORTS,
) -> BrowserValidationResult:
    """Render and interact with approved helpers in a real Chromium browser."""

    browser = find_chromium(browser_path)
    if browser is None:
        raise PublishFailedError(
            "Browser validation needs Chrome, Chromium, or Edge. Set HIGH2MIN_BROWSER to its executable."
        )
    results: list[BrowserViewportResult] = []
    with tempfile.TemporaryDirectory(prefix="high2min-browser-") as temporary:
        root = Path(temporary)
        (root / "index.html").write_text(_harness_html(), encoding="utf-8")
        for name in ("media-playback-independence.js", "sign-language-video.js", "sign-language-video.css"):
            (root / name).write_bytes(files("adt_video_publisher").joinpath("assets", name).read_bytes())
        url = (root / "index.html").resolve().as_uri()
        for width, height in viewports:
            profile = root / f"profile-{width}"
            command = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--allow-file-access-from-files",
                "--autoplay-policy=no-user-gesture-required",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "--virtual-time-budget=3000",
                "--dump-dom",
                url,
            ]
            if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
                command.insert(1, "--no-sandbox")
            try:
                completed = _run_browser_command(command)
            except (OSError, subprocess.SubprocessError) as exc:
                raise PublishFailedError(f"Headless browser validation could not run: {exc}") from exc
            match = re.search(
                r'data-high2min-browser-result="(?P<value>\{.*?\})"',
                completed.stdout,
                re.DOTALL,
            )
            if completed.returncode != 0 or match is None:
                error_match = re.search(r'data-high2min-browser-error="(?P<value>[^"]+)"', completed.stdout)
                if error_match:
                    detail = html.unescape(error_match.group("value"))
                else:
                    detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "no result"
                raise PublishFailedError(
                    f"Headless browser validation failed at {width}x{height}: {detail}"
                )
            try:
                checks = json.loads(html.unescape(match.group("value")))
            except json.JSONDecodeError as exc:
                raise PublishFailedError("Headless browser returned an invalid validation result.") from exc
            if not isinstance(checks, dict) or any(not isinstance(value, bool) for value in checks.values()):
                raise PublishFailedError("Headless browser returned an incomplete validation result.")
            result = BrowserViewportResult(width=width, height=height, checks=checks)
            if not result.passed:
                failed = ", ".join(name for name, passed in checks.items() if not passed)
                raise PublishFailedError(f"Browser contract failed at {width}x{height}: {failed}.")
            results.append(result)
    return BrowserValidationResult(browser=browser, viewports=tuple(results))
