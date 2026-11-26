"""Local 'Creative Cloud Files' sync folder.

The announcement recommends downloading from the desktop app rather than the
web, because the web UI only hands you one file at a time. This source takes
that to its conclusion: whatever CoreSync has already placed on disk is
copied wholesale, which is both faster and more reliable than the web API.

Partial sync placeholders (0-byte stubs) are detected and reported instead of
being copied silently.
"""

from __future__ import annotations

from typing import Iterator

from adobe_rescue import config
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG, human_size, iter_files

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".ccsync", ".cctemp"}
#: Shortcuts are not content; counting one as a backed-up item is a lie.
SKIP_SUFFIXES = {".lnk", ".url", ".alias"}


def _tidy(folder_name: str) -> str:
    """Drop the account email and user id Adobe appends to the sync folder.

    Keeps the backup shareable: "Creative Cloud Files  user@x.com ABC123@..."
    becomes "Creative Cloud Files".
    """
    import re
    name = re.split(r"\s{2,}|\s+\S+@", folder_name)[0].strip()
    return name or folder_name


class LocalSync(Source):
    name = "local-sync"
    title = "Yerel Creative Cloud Files klasörü"
    needs_auth = False
    empty_hint = ("Yerel 'Creative Cloud Files' klasörü bulunamadı. Creative Cloud masaüstü "
                  "uygulamasını kurup oturum açın ve senkronun bitmesini bekleyin.")

    def available(self, ctx: Context) -> bool:
        return bool(config.creative_cloud_sync_dirs())

    def discover(self, ctx: Context) -> Iterator[Item]:
        roots = config.creative_cloud_sync_dirs()
        if not roots:
            LOG.warning(self.empty_hint)
            return
        yielded = 0
        for root in roots:
            # The folder name embeds the account email and user id; keep that
            # out of a log file people may share.
            LOG.info("  Yerel klasör: %s", _tidy(root.name))
            stubs = 0
            for source_file in iter_files(root, skip_names=SKIP_NAMES):
                try:
                    stat = source_file.stat()
                    rel = source_file.relative_to(root)
                except (OSError, ValueError):
                    continue
                if stat.st_size == 0:
                    stubs += 1
                if source_file.suffix.lower() in SKIP_SUFFIXES:
                    continue
                yielded += 1
                yield Item(
                    key=f"{self.name}:{root.name}/{rel.as_posix()}",
                    name=source_file.name,
                    parts=[_tidy(root.name), *rel.parts[:-1]],
                    size=stat.st_size,
                    local_path=source_file,
                    modified=str(int(stat.st_mtime)),
                    note="0 bayt — senkron tamamlanmamış olabilir" if stat.st_size == 0 else "",
                )
            if stubs:
                LOG.warning("  ! %s içinde %d adet 0 baytlık dosya var — "
                            "senkron bitmemiş olabilir.", _tidy(root.name), stubs)

        if roots and not yielded:
            # The folder exists but holds only a shortcut: saying "not found"
            # here is simply untrue and sends people to reinstall Creative
            # Cloud for no reason.
            self.empty_hint = (
                "Yerel senkron klasörü var ama içinde dosya yok (yalnızca kısayol). "
                "Bulut dosyalarınız 'Creative Cloud Dosyaları' bölümünde yedeklendi.")


class LocalExtras(Source):
    """Lightroom Classic catalogs and other heavy local artefacts worth a copy."""

    name = "local-extras"
    title = "Lightroom Classic kataloğu ve diğer yerel dosyalar"
    needs_auth = False

    def discover(self, ctx: Context) -> Iterator[Item]:
        home = config.home()
        patterns = [
            ("lightroom-classic", home / "Pictures", "*.lrcat"),
            ("lightroom-classic", home / "Resimler", "*.lrcat"),
            ("lightroom-classic", home / "Pictures" / "Lightroom", "*.lrcat"),
        ]
        seen = set()
        for label, base, pattern in patterns:
            if not base.exists():
                continue
            try:
                matches = list(base.rglob(pattern))
            except OSError:
                continue
            for match in matches[:50]:
                if match in seen or not match.is_file():
                    continue
                seen.add(match)
                size = match.stat().st_size
                LOG.info("  Katalog bulundu: %s (%s)", match.name, human_size(size))
                yield Item(
                    key=f"{self.name}:{match.as_posix()}",
                    name=match.name,
                    parts=[label],
                    size=size,
                    local_path=match,
                    note="Lightroom Classic kataloğu — yeni hesapta da aynı katalogla devam edebilirsiniz.",
                )
