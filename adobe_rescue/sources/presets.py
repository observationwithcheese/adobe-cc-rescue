"""Application presets: brushes, actions, swatches, workspaces, scripts.

Point 4 of the announcement ("Ayarlar"): these live only on the workstation,
never in the cloud, and a reset account does not bring them back. We copy the
useful preset/settings subtrees and skip caches, previews and temp data.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterator, List

from adobe_rescue import config
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG, human_size, iter_files

#: Only these extensions are worth carrying over.
KEEP_EXT = {
    ".abr", ".atn", ".asl", ".aco", ".ase", ".act", ".grd", ".pat", ".shc", ".tpl",
    ".csh", ".psp", ".ai", ".ait", ".idml", ".indt", ".jsx", ".jsxbin", ".scpt",
    ".xml", ".json", ".txt", ".prst", ".look", ".cube", ".sequence", ".kys",
    ".mnu", ".workspace", ".aep", ".ffx", ".prfpset", ".epr", ".lrtemplate", ".xmp",
}
SKIP_DIRS = {
    "Cache", "Caches", "Media Cache", "Media Cache Files", "Peak Files",
    "Logs", "Temp", "TempFiles", "Crash", "CrashReports", "Backup",
    "Previews.lrdata", "Thumbnails", "node_modules",
}
MAX_FILE_MB = 200


class Presets(Source):
    name = "presets"
    title = "Uygulama ön ayarları (fırça, aksiyon, renk örneği)"
    needs_auth = False
    empty_hint = ("Ön ayar klasörü bulunamadı — Adobe uygulamaları bu bilgisayarda kurulu "
                  "değilse bu normaldir.")

    def available(self, ctx: Context) -> bool:
        return bool(config.preset_locations())

    def discover(self, ctx: Context) -> Iterator[Item]:
        locations = config.preset_locations()
        if not locations:
            LOG.warning("Bu platform için ön ayar yolu tanımlı değil.")
            return

        for label, patterns in locations.items():
            for pattern in patterns:
                for base in _expand(pattern):
                    if not base.exists():
                        continue
                    LOG.info("  Ön ayarlar: %s", base)
                    for file in iter_files(base):
                        if _skip(file, base):
                            continue
                        try:
                            size = file.stat().st_size
                            rel = file.relative_to(base)
                        except (OSError, ValueError):
                            continue
                        if size > MAX_FILE_MB * 1024 * 1024:
                            LOG.debug("  atlandı (büyük): %s %s", file.name, human_size(size))
                            continue
                        yield Item(
                            key=f"{self.name}:{label}/{base.name}/{rel.as_posix()}",
                            name=file.name,
                            parts=[label, base.name, *rel.parts[:-1]],
                            size=size,
                            local_path=file,
                        )


def _expand(pattern: Path) -> List[Path]:
    """Expand a path whose final component may contain a glob (e.g. 'Adobe Photoshop*')."""
    text = str(pattern)
    if "*" not in text:
        return [pattern]
    parent = pattern.parent
    needle = pattern.name
    if "*" in str(parent):               # glob in the middle — walk from the top
        root = Path(str(parent).split("*")[0]).parent
        try:
            return [p for p in root.rglob("*") if p.is_dir() and fnmatch.fnmatch(str(p), text)][:20]
        except OSError:
            return []
    if not parent.exists():
        return []
    try:
        return [p for p in parent.iterdir() if p.is_dir() and fnmatch.fnmatch(p.name, needle)]
    except OSError:
        return []


def _skip(file: Path, base: Path) -> bool:
    try:
        rel_parts = set(file.relative_to(base).parts[:-1])
    except ValueError:
        return True
    if rel_parts & SKIP_DIRS:
        return True
    return file.suffix.lower() not in KEEP_EXT
