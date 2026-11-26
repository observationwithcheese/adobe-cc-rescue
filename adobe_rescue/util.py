"""Small shared helpers: logging, paths, sizes, hashing, archiving."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional, Sequence

LOG = logging.getLogger("adobe_rescue")

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _utf8_console() -> None:
    """Stop Turkish output from killing the CLI on a cp1252 Windows console.

    Without this, a single 'ğ' in a log line raises UnicodeEncodeError from
    deep inside logging and takes the command with it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def setup_logging(verbose: bool = False, logfile: Optional[Path] = None) -> None:
    _utf8_console()
    level = logging.DEBUG if verbose else logging.INFO
    LOG.setLevel(level)
    LOG.handlers.clear()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(message)s"))
    stream.setLevel(level)
    LOG.addHandler(stream)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        fh.setLevel(logging.DEBUG)
        LOG.addHandler(fh)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "süresi doldu"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts: List[str] = []
    if days:
        parts.append(f"{days}g")
    if hours or days:
        parts.append(f"{hours}s")
    parts.append(f"{minutes}dk")
    return " ".join(parts)


def safe_name(name: str, max_len: int = 120) -> str:
    """Make a single path component safe on Windows and macOS.

    Keeps Turkish characters (NFC-normalised) — only genuinely illegal
    characters are replaced.
    """
    name = unicodedata.normalize("NFC", name or "untitled")
    name = _ILLEGAL.sub("_", name)
    # Strip every kind of trailing whitespace, not just ASCII space: Adobe
    # allows names like "outputs\u00a0" (no-break space), while Windows
    # silently strips trailing spaces and dots — which would otherwise make
    # the written path differ from the recorded one.
    name = name.strip().strip(" .\u00a0\u2007\u202f\u200b\ufeff\t").strip()
    if not name:
        name = "untitled"
    stem, dot, ext = name.rpartition(".")
    if dot and (stem.upper() in _WINDOWS_RESERVED):
        stem = f"{stem}_"
        name = f"{stem}.{ext}"
    elif name.upper() in _WINDOWS_RESERVED:
        name = f"{name}_"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def safe_path(root: Path, parts: Sequence[str]) -> Path:
    """Join user-controlled parts under root, defeating traversal."""
    path = root
    for part in parts:
        for chunk in str(part).replace("\\", "/").split("/"):
            if chunk in ("", ".", ".."):
                continue
            path = path / safe_name(chunk)
    resolved_root = root.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(resolved_root)
    except (ValueError, OSError):
        return root / safe_name("_".join(str(p) for p in parts))
    return path


def unique_path(path: Path) -> Path:
    """Return a non-colliding path by appending (1), (2)..."""
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    for i in range(1, 10000):
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{os.getpid()}{ext}")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_space_gb(path: Path) -> float:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free / (1024 ** 3)
    except OSError:
        return float("inf")


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def iter_files(path: Path, skip_names: Iterable[str] = ()) -> Iterator[Path]:
    skip = set(skip_names)
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name in skip:
                continue
            yield Path(root) / name


def copy_tree(src: Path, dst: Path, on_file: Optional[Callable[[Path], None]] = None) -> int:
    """Copy a directory tree, skipping unreadable files instead of aborting."""
    copied = 0
    for source in iter_files(src, skip_names={".DS_Store", "Thumbs.db"}):
        try:
            rel = source.relative_to(src)
        except ValueError:
            continue
        target = safe_path(dst, rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
            copied += 1
            if on_file:
                on_file(source)
        except (OSError, shutil.Error) as exc:
            LOG.warning("  ! kopyalanamadı %s (%s)", source, exc)
    return copied


def zip_dir(src: Path, archive: Path, compress: bool = True) -> Path:
    """Zip a folder. Uses ZIP64 so >4 GB backups are fine."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(archive, "w", mode, allowZip64=True) as zf:
        for file in iter_files(src):
            try:
                zf.write(file, file.relative_to(src))
            except (OSError, ValueError) as exc:
                LOG.warning("  ! arşive eklenemedi %s (%s)", file, exc)
    return archive


def open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception as exc:  # pragma: no cover - best effort
        LOG.debug("klasör açılamadı: %s", exc)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
