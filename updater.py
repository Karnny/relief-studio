"""In-app updater for Relief Studio.

The GitHub Release is the update channel: the app asks for the latest release,
compares its tag with the running ``APP_VERSION``, and - for the frozen Windows
build - downloads ``ReliefStudio.exe`` next to the app's update directory,
verifies the SHA-256 published alongside the asset, and stages it. On the next
start the running exe is renamed aside, the staged exe takes its place and the
new build is launched (Windows allows renaming a running executable, only
overwriting/deleting it is blocked).

Everything here is stdlib only; the functions that touch the network take the
URL/timeout as arguments so they can be pointed at a local fake release in
tests (``RELIEF_UPDATE_API``) and never silently talk to the internet.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_API = "https://api.github.com/repos/Karnny/relief-studio/releases/latest"
ASSET_NAME = "ReliefStudio.exe"
SHA_NAME = ASSET_NAME + ".sha256"
USER_AGENT = "ReliefStudio-updater"
MAX_DOWNLOAD_BYTES = 160 * 1024 * 1024
HTTP_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 180


class UpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------- versions
def parse_version(text) -> tuple[int, int, int]:
    """'v1.5.0' / '1.5' / '2026.11.16' -> comparable tuple; ValueError if bad."""
    value = str(text or "").strip()
    if value[:1].lower() == "v":
        value = value[1:]
    parts = value.split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a version: {text!r}")
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])          # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------- network
def _url_allowed(url: str, insecure: bool) -> bool:
    url = (url or "").strip().lower()
    if url.startswith("https://"):
        return True
    return insecure and url.startswith("http://")


def fetch_bytes(url: str, *, timeout: float = HTTP_TIMEOUT,
                max_bytes: int = 1024 * 1024, insecure: bool = False) -> bytes:
    if not _url_allowed(url, insecure):
        raise UpdateError("refusing a non-HTTPS update URL")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            chunks = []
            total = 0
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise UpdateError("download is larger than expected")
                chunks.append(block)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError("GitHub rate limit hit - try again later") from exc
        raise UpdateError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise UpdateError("update request timed out") from exc


def parse_sha_text(text: str) -> str:
    for token in str(text).replace("\r", "\n").split():
        token = token.strip()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    raise UpdateError("checksum file has no SHA-256")


def parse_release(data: dict, *, insecure: bool = False) -> dict:
    """GitHub ``releases/latest`` JSON -> release description."""
    tag = str(data.get("tag_name") or data.get("name") or "")
    version = ".".join(str(n) for n in parse_version(tag))
    assets = data.get("assets") or []
    asset = sha_asset = None
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name.lower() == ASSET_NAME.lower():
            asset = item
        elif name.lower() == SHA_NAME.lower():
            sha_asset = item
    if asset is None:
        raise UpdateError("latest release has no ReliefStudio.exe asset")
    sha = None
    if sha_asset is not None:
        raw = fetch_bytes(str(sha_asset.get("browser_download_url") or ""),
                          timeout=HTTP_TIMEOUT, max_bytes=4096, insecure=insecure)
        sha = parse_sha_text(raw.decode("utf-8", "replace"))
    return {
        "latest": version,
        "asset_url": str(asset.get("browser_download_url") or ""),
        "asset_size": int(asset.get("size") or 0),
        "sha256": sha,
        "page_url": str(data.get("html_url") or ""),
        "notes": str(data.get("body") or "")[:2000],
        "published_at": str(data.get("published_at") or ""),
    }


def fetch_latest(api_url: str = DEFAULT_API, *, insecure: bool = False,
                 timeout: float = HTTP_TIMEOUT) -> dict:
    raw = fetch_bytes(api_url, timeout=timeout, max_bytes=2 * 1024 * 1024,
                      insecure=insecure)
    try:
        data = json.loads(raw.decode("utf-8-sig"))      # tolerate a BOM from mirrors
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("update feed is not valid JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("update feed has an unexpected shape")
    return parse_release(data, insecure=insecure)


def check_update(current_version: str, api_url: str = DEFAULT_API, *,
                 insecure: bool = False, timeout: float = HTTP_TIMEOUT) -> dict:
    release = fetch_latest(api_url, insecure=insecure, timeout=timeout)
    newer = is_newer(release["latest"], current_version)
    return {
        "current": current_version,
        "latest": release["latest"],
        "update_available": newer,
        "can_install": bool(newer and release.get("sha256")),
        "page_url": release["page_url"],
        "asset_size": release["asset_size"],
        "notes": release["notes"],
        "published_at": release["published_at"],
        "release": release,
        "error": None,
    }


# ---------------------------------------------------------------- staging
def staging_root() -> str:
    """Per-user update directory (``%LOCALAPPDATA%\\ReliefStudio\\updates``)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "ReliefStudio", "updates")


def _version_dir(root: str, version: str) -> str:
    safe = "".join(c for c in version if c.isdigit() or c == ".")
    if not safe:
        raise UpdateError("bad release version")
    return os.path.join(root, safe)


def download_to_staging(release: dict, root: str | None = None, *,
                        insecure: bool = False) -> dict:
    """Download + verify the release exe and leave it staged for the next start."""
    version = release["latest"]
    sha = str(release.get("sha256") or "")
    if not sha:
        raise UpdateError("release has no checksum asset")
    root = root or staging_root()
    dest_dir = _version_dir(root, version)
    os.makedirs(dest_dir, exist_ok=True)
    data = fetch_bytes(str(release.get("asset_url") or ""), timeout=DOWNLOAD_TIMEOUT,
                       max_bytes=MAX_DOWNLOAD_BYTES, insecure=insecure)
    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != sha.lower():
        raise UpdateError("download checksum does not match the published SHA-256")
    dest = os.path.join(dest_dir, ASSET_NAME)
    partial = dest + ".part"
    with open(partial, "wb") as f:
        f.write(data)
    os.replace(partial, dest)
    meta = {"version": version, "sha256": digest, "page_url": release.get("page_url", ""),
            "staged_at": time.time()}
    with open(os.path.join(dest_dir, "version.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    _prune_other_versions(root, version)
    return {"version": version, "path": dest, "bytes": len(data), "sha256": digest}


def _prune_other_versions(root: str, keep: str) -> None:
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for name in entries:
        if name == keep:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def pending_update(current_version: str, root: str | None = None) -> dict | None:
    """Return the newest staged update newer than ``current_version``, if any."""
    root = root or staging_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return None
    best = None
    for name in entries:
        dest_dir = os.path.join(root, name)
        meta_path = os.path.join(dest_dir, "version.json")
        exe = os.path.join(dest_dir, ASSET_NAME)
        if not (os.path.isdir(dest_dir) and os.path.isfile(meta_path) and os.path.isfile(exe)):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            version = str(meta.get("version") or "")
            if is_newer(version, current_version):
                if best is None or is_newer(version, best["version"]):
                    best = {"version": version, "path": exe, "meta": meta}
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return best


# ---------------------------------------------------------------- applying
def swap_executable(exe_path: str, staged_path: str) -> str:
    """Put the staged exe in place of the running one; returns the backup path.

    Renaming the running executable is allowed on Windows; overwriting is not, so
    the old file is moved aside first. On failure the old exe is restored.
    """
    exe_path = os.path.abspath(exe_path)
    staged_path = os.path.abspath(staged_path)
    old = exe_path + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    os.replace(exe_path, old)
    try:
        os.replace(staged_path, exe_path)
    except OSError:
        try:
            os.replace(old, exe_path)
        except OSError:
            pass
        raise
    return old


def launch_detached(exe_path: str) -> None:
    kwargs = {"close_fds": True, "env": child_environment()}
    if sys.platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                                   getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([exe_path], **kwargs)


def child_environment() -> dict:
    """Environment for relaunching the frozen exe as a normal parent instance.

    A PyInstaller one-file process exports ``_PYI_*`` variables for its
    bootloader child. If a relaunched copy inherits them it boots in *child*
    mode and looks for python DLLs in the old ``_MEI`` extraction directory,
    which the exiting instance may already have deleted -> silent start failure.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def cleanup_stale_helpers(max_age_s: float = 3600.0) -> int:
    """Remove leftover restart .cmd helpers from %TEMP% (self-delete often fails)."""
    root = tempfile.gettempdir()
    removed = 0
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not (name.startswith("relief-restart-") and name.endswith(".cmd")):
            continue
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) > max_age_s:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def cleanup_obsolete(exe_path: str) -> None:
    old = exe_path + ".old"
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass


def apply_pending_update(current_version: str, exe_path: str | None = None) -> str | None:
    """Swap in a staged update and start the new build.

    Returns the backup path when a swap happened (the caller should exit so the
    freshly launched exe takes over), or None when nothing is staged.
    """
    exe_path = os.path.abspath(exe_path or sys.executable)
    pending = pending_update(current_version)
    if pending is None:
        return None
    old = swap_executable(exe_path, pending["path"])
    launch_detached(exe_path)
    return old
