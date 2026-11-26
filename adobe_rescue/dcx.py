"""Read what is usable out of a downloaded cloud document package.

A .psdc is a UCF zip holding Adobe's per-layer CPSD data — there is no PSD
stream inside it, and the layer codec is not something to reverse engineer
against a deadline. What the package *does* carry is a flattened rendition,
and for many documents that rendition is at full document resolution.

That is a real salvage: it needs no Adobe account, works offline, and keeps
working after the migration. It is flattened, so it does not replace a PSD —
the report says so plainly.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from adobe_rescue.util import LOG

SIZE_IN_NAME = re.compile(r"(\d{2,6})[x\-_](\d{2,6})")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def _document_size(zf: zipfile.ZipFile) -> Tuple[int, int]:
    """Document dimensions from the manifest, or (0, 0)."""
    for name in ("manifest", "Manifest", "manifest.json"):
        if name not in zf.namelist():
            continue
        try:
            data = json.loads(zf.read(name).decode("utf-8", "replace"))
        except (ValueError, KeyError):
            return 0, 0
        cols = rows = 0
        for key, value in data.items():
            low = str(key).lower()
            if low.endswith("cols") or low.endswith("width"):
                cols = int(value or 0)
            elif low.endswith("rows") or low.endswith("height"):
                rows = int(value or 0)
        return cols, rows
    return 0, 0


def _candidates(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    out = []
    for info in zf.infolist():
        name = info.filename.lower()
        if name.endswith(IMAGE_SUFFIXES) and ("rendition" in name or "preview" in name
                                              or "thumb" in name or SIZE_IN_NAME.search(name)):
            out.append(info)
    return out


def _pixels(info: zipfile.ZipInfo) -> int:
    match = SIZE_IN_NAME.search(info.filename)
    if match:
        return int(match.group(1)) * int(match.group(2))
    return info.file_size      # fall back to bytes as a proxy


def describe(package: Path) -> Dict[str, object]:
    """Document size, best rendition size, and whether it is full resolution."""
    info: Dict[str, object] = {"ok": False}
    if not zipfile.is_zipfile(package):
        return info
    try:
        with zipfile.ZipFile(package) as zf:
            cols, rows = _document_size(zf)
            best = max(_candidates(zf), key=_pixels, default=None)
            if best is None:
                return {"ok": False, "cols": cols, "rows": rows}
            match = SIZE_IN_NAME.search(best.filename)
            width = int(match.group(1)) if match else 0
            height = int(match.group(2)) if match else 0
            return {
                "ok": True,
                "cols": cols, "rows": rows,
                "width": width, "height": height,
                "entry": best.filename,
                "full_resolution": bool(cols and width and width >= cols and height >= rows),
            }
    except (OSError, zipfile.BadZipFile) as exc:
        LOG.debug("paket okunamadı %s: %s", package, exc)
    return info


def extract_rendition(package: Path, out_dir: Path,
                      stem: Optional[str] = None) -> Optional[Path]:
    """Write the largest flattened image the package holds. Returns its path."""
    if not zipfile.is_zipfile(package):
        return None
    try:
        with zipfile.ZipFile(package) as zf:
            best = max(_candidates(zf), key=_pixels, default=None)
            if best is None:
                return None
            blob = zf.read(best)
            cols, rows = _document_size(zf)
            match = SIZE_IN_NAME.search(best.filename)
            width = int(match.group(1)) if match else 0
            suffix = Path(best.filename).suffix or ".png"
            name = stem or package.stem
            tag = f" ({width}px)" if width else ""
            if cols and width and width >= cols:
                tag = " (tam çözünürlük)"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{name}{tag}{suffix}"
            target.write_bytes(blob)
            return target
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        LOG.debug("önizleme çıkarılamadı %s: %s", package, exc)
    return None
