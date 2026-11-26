"""The backup engine: discover → filter → transfer → verify → record."""

from __future__ import annotations

import fnmatch
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from adobe_rescue.auth import ApiKeyError, AuthError, Session as AdobeSession
from adobe_rescue.config import Settings
from adobe_rescue.http import Client
from adobe_rescue.manifest import (Entry, Manifest, STATUS_DONE, STATUS_FAILED,
                                   STATUS_MANUAL, STATUS_SKIPPED)
from adobe_rescue.sources import Context, Item, Source, build
from adobe_rescue.util import (LOG, free_space_gb, human_size, safe_name,
                               safe_path, sha256_file)

ProgressFn = Callable[["Progress"], None]


@dataclass
class Progress:
    source: str = ""
    item: str = ""
    done: int = 0
    total: int = 0
    bytes_done: int = 0          # completed files
    bytes_expected: int = 0      # this source's queue
    bytes_inflight: int = 0      # partial transfers right now
    speed: float = 0.0           # bytes/sec over the whole source
    failed: int = 0
    message: str = ""

    @property
    def bytes_seen(self) -> int:
        return self.bytes_done + self.bytes_inflight

    def eta_seconds(self) -> float:
        remaining = max(self.bytes_expected - self.bytes_seen, 0)
        return remaining / self.speed if self.speed > 1024 else 0.0


@dataclass
class Result:
    discovered: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    manual: int = 0
    bytes_total: int = 0
    errors: List[str] = field(default_factory=list)
    per_source: Dict[str, int] = field(default_factory=dict)


class Engine:
    def __init__(self, settings: Settings, session: Optional[AdobeSession] = None,
                 on_progress: Optional[ProgressFn] = None):
        self.settings = settings
        self.session = session
        self.on_progress = on_progress
        self.root = Path(settings.output)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest(self.root)
        self.manifest.read_only = bool(settings.dry_run)
        self.client: Optional[Client] = None
        if session and session.token:
            self.client = Client(session, settings)
        self.ctx = Context(settings=settings, manifest=self.manifest,
                           root=self.root, client=self.client)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.progress = Progress()
        self._inflight: Dict[str, int] = {}
        self._source_started = 0.0

    # ------------------------------------------------------------- control
    def stop(self) -> None:
        self._stop.set()

    def _emit(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self.progress, key, value)
        if self.on_progress:
            try:
                self.on_progress(self.progress)
            except Exception:  # pragma: no cover - UI must never kill the run
                pass

    # ------------------------------------------------------------- filters
    def _excluded(self, item: Item) -> Optional[str]:
        settings = self.settings
        rel = "/".join([*item.parts, item.name])
        if settings.include and not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(item.name, pat)
                                        for pat in settings.include):
            return "include filtresi dışında"
        for pat in settings.exclude:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(item.name, pat):
                return f"exclude filtresi: {pat}"
        if settings.max_size_mb and item.size > settings.max_size_mb * 1024 * 1024:
            return f"boyut sınırı aşıldı ({human_size(item.size)})"
        return None

    # --------------------------------------------------------------- run
    def run(self, source_names: List[str]) -> Result:
        result = Result()
        sources = build(source_names)

        for source in sources:
            if self._stop.is_set():
                break
            if source.needs_auth and self.client is None:
                LOG.info("· %s atlandı (oturum yok)", source.title)
                continue
            if not source.available(self.ctx):
                LOG.info("· %s atlandı (%s)", source.title, source.empty_hint or "kullanılamıyor")
                continue

            LOG.info("")
            LOG.info("▸ %s", source.title)
            self._emit(source=source.title, message="taranıyor…", item="",
                       done=0, total=0)

            try:
                items = list(source.discover(self.ctx))
            except ApiKeyError as exc:
                # Adobe issues a client id per service and will not give this
                # one to a third-party tool. That is not a failure the user
                # can retry — it is a manual step, so say what to do instead.
                LOG.warning("  ! %s: Adobe erişim vermedi", source.title)
                LOG.debug("  ayrıntı: %s", exc)
                hint = (source.access_denied_hint
                        or f"{source.title} bu araçla indirilemiyor; Adobe'nin web "
                           f"arayüzünden elle indirin.")
                entry = Entry(key=f"{source.name}:__noaccess__", source=source.name,
                              name=f"{source.title} — elle indirilmeli", size=0)
                entry.mark(STATUS_MANUAL, hint)
                self.manifest.add(entry)
                self.manifest.update(entry)
                result.manual += 1
                continue
            except AuthError:
                raise
            except Exception as exc:
                LOG.error("  ! %s taranamadı: %s", source.title, exc)
                result.errors.append(f"{source.name}: {exc}")
                continue

            if not items:
                # A source that was enabled, had a session and still returned
                # nothing is a failure, not a quiet success. Record it so the
                # report cannot read as "0 hatalı" while half the account was
                # never backed up.
                entry = Entry(key=f"{source.name}:__empty__", source=source.name,
                              name=f"[{source.title}] hiç öğe bulunamadı", size=0)
                note = (source.empty_hint or "Kaynak hiç öğe döndürmedi.")
                if source.needs_auth:
                    # Cloud sources exist because the account has cloud data.
                    # Returning nothing means access failed, not that the
                    # account is empty — that must be loud.
                    LOG.warning("  ! HİÇ ÖĞE BULUNAMADI — %s", note)
                    entry.mark(STATUS_FAILED,
                               note + " Bulut erişimi başarısız olmuş olabilir — oturumu "
                                      "yenileyip tekrar deneyin; bu kaynak YEDEKLENMEDİ.")
                    result.failed += 1
                    result.errors.append(f"{source.name}: hiç öğe bulunamadı")
                else:
                    # A local source legitimately finds nothing (no Lightroom
                    # Classic installed, say). Record it, do not alarm.
                    LOG.info("  (öğe yok) %s", note)
                    entry.mark(STATUS_SKIPPED, note)
                    result.skipped += 1
                self.manifest.add(entry)
                self.manifest.update(entry)
                continue

            # A previous run may have recorded "nothing found" for this
            # source. It found something now, so that marker is a lie.
            self.manifest.drop(f"{source.name}:__empty__")

            manual_count = sum(1 for i in items if i.manual)
            fetch = [i for i in items if not i.manual]
            total_bytes = sum(i.size for i in fetch)
            if manual_count:
                LOG.info("  %d öğe: %d indirilecek (%s) + %d elle yapılacak",
                         len(items), len(fetch), human_size(total_bytes), manual_count)
            else:
                LOG.info("  %d öğe bulundu (%s)", len(items), human_size(total_bytes))
            result.discovered += len(items)
            result.per_source[source.name] = len(items)

            self._transfer(source, items, result)
            self.manifest.save(force=True)

        self.manifest.save(force=True)
        return result

    # ---------------------------------------------------------- transfers
    def _transfer(self, source: Source, items: List[Item], result: Result) -> None:
        out_dir = self.ctx.source_dir(source.name)
        queue: List[Item] = []

        for item in items:
            entry = Entry(key=item.key, source=source.name, name=item.name,
                          size=item.size, modified=item.modified, note=item.note,
                          url=item.url)

            if item.manual:
                entry.mark(STATUS_MANUAL)
                self.manifest.add(entry)
                result.manual += 1
                continue

            reason = self._excluded(item)
            if reason:
                entry.mark(STATUS_SKIPPED, reason)
                self.manifest.add(entry)
                result.skipped += 1
                continue

            target = safe_path(out_dir, [*item.parts, safe_name(item.name)])
            entry.path = str(target.relative_to(self.root))

            if self.settings.resume and not self.settings.overwrite and target.exists():
                existing = self.manifest.get(item.key)
                # A cloud document is assembled per job, so its size and
                # checksum differ between runs even when nothing changed.
                # Trust the manifest for those instead of comparing sizes,
                # or every resumed run would fetch all 740 MB again.
                if item.meta.get("composite"):
                    already = bool(existing and existing.status == STATUS_DONE)
                else:
                    already = item.size == 0 or target.stat().st_size == item.size
                if already:
                    entry.sha256 = existing.sha256 if existing else entry.sha256
                    entry.size = existing.size if existing else entry.size
                    entry.mark(STATUS_DONE, "zaten mevcut")
                    self.manifest.add(entry)
                    result.skipped += 1
                    continue

            self.manifest.add(entry)
            item.meta["_target"] = target
            queue.append(item)

        if not queue:
            LOG.info("  tamamı zaten indirilmiş (%d öğe atlandı)", len(items))
            self._emit(message="güncel", item="")
            return

        # Largest first: a huge file at the end looks like a hang, and it
        # packs the parallel workers better.
        queue.sort(key=lambda i: i.size, reverse=True)

        needed_gb = sum(i.size for i in queue) / (1024 ** 3)
        free_gb = free_space_gb(self.root)
        if free_gb < needed_gb + self.settings.min_free_gb:
            LOG.warning("  ! Disk alanı yetersiz olabilir: gereken ~%.1f GB, boş %.1f GB",
                        needed_gb, free_gb)

        if self.settings.dry_run:
            LOG.info("  [deneme] %d öğe indirilecekti (%s)",
                     len(queue), human_size(sum(i.size for i in queue)))
            return

        already = len(items) - len(queue) - sum(1 for i in items if i.manual)
        if already > 0:
            LOG.info("  %d öğe zaten indirilmiş, atlanıyor", already)

        total = len(queue)
        counter = {"done": 0}
        expected = sum(i.size for i in queue)
        self._inflight.clear()
        self._source_started = time.monotonic()
        self._emit(total=total, done=0, bytes_expected=expected,
                   bytes_inflight=0, speed=0.0)

        def work(item: Item) -> None:
            if self._stop.is_set():
                return
            entry = self.manifest.get(item.key)
            assert entry is not None
            target: Path = item.meta["_target"]
            entry.attempts += 1
            try:
                if item.is_local:
                    size = self._copy_local(item, target)
                else:
                    size = self._download(item, target)
                entry.size = size or entry.size
                if self.settings.verify and target.exists():
                    entry.sha256 = sha256_file(target)
                if item.meta.get("composite") and target.exists():
                    self._salvage_rendition(item, target, entry)
                entry.mark(STATUS_DONE)
                with self._lock:
                    result.downloaded += 1
                    result.bytes_total += entry.size
            except AuthError:
                entry.mark(STATUS_FAILED, "oturum süresi doldu")
                self.manifest.update(entry)
                self._stop.set()
                raise
            except Exception as exc:
                if item.meta.get("optional"):
                    # A missing preview is not a backup failure.
                    entry.mark(STATUS_SKIPPED, f"önizleme alınamadı: {str(exc)[:120]}")
                    with self._lock:
                        result.skipped += 1
                    LOG.debug("  önizleme atlandı: %s (%s)", item.name, exc)
                else:
                    entry.mark(STATUS_FAILED, str(exc)[:300])
                    with self._lock:
                        result.failed += 1
                        result.errors.append(f"{item.name}: {exc}")
                    LOG.warning("  ! %s — %s", item.name, exc)
            finally:
                self.manifest.update(entry)
                with self._lock:
                    counter["done"] += 1
                self._emit(item=item.name, done=counter["done"], total=total,
                           bytes_done=result.bytes_total, failed=result.failed,
                           message="aktarılıyor")

        workers = 1 if any(i.is_local for i in queue) else max(1, self.settings.workers)
        if workers == 1:
            for item in queue:
                if self._stop.is_set():
                    break
                work(item)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, item) for item in queue]
                for future in as_completed(futures):
                    if self._stop.is_set():
                        break
                    exc = future.exception()
                    if isinstance(exc, AuthError):
                        for f in futures:
                            f.cancel()
                        raise exc

        LOG.info("  bitti: %d/%d (%s)", counter["done"], total, human_size(result.bytes_total))

    def _salvage_rendition(self, item: Item, package: Path, entry: Entry) -> None:
        """Pull the flattened image out of a cloud-document package.

        It is the only part of the document that opens without Adobe, so it
        is extracted locally the moment the package lands.
        """
        from adobe_rescue import dcx

        try:
            facts = dcx.describe(package)
            out_dir = package.parent.parent / "_duz-gorseller"
            written = dcx.extract_rendition(package, out_dir, stem=package.stem)
        except Exception as exc:          # never let salvage break a backup
            LOG.debug("önizleme çıkarılamadı: %s", exc)
            return
        if not written:
            return
        if facts.get("full_resolution"):
            entry.note = (f"{entry.note} Düz görsel tam çözünürlükte çıkarıldı: "
                          f"_duz-gorseller/{written.name}").strip()
        else:
            entry.note = (f"{entry.note} Düz görsel çıkarıldı "
                          f"({facts.get('width') or '?'}px, belge "
                          f"{facts.get('cols') or '?'}px): "
                          f"_duz-gorseller/{written.name}").strip()

    def _copy_local(self, item: Item, target: Path) -> int:
        assert item.local_path is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.local_path, target)
        return target.stat().st_size

    def _report_bytes(self, key: str, done: int) -> None:
        with self._lock:
            self._inflight[key] = done
            inflight = sum(self._inflight.values())
            elapsed = max(time.monotonic() - self._source_started, 0.001)
            moved = self.progress.bytes_done + inflight
        self._emit(bytes_inflight=inflight, speed=moved / elapsed)

    def _download(self, item: Item, target: Path) -> int:
        assert self.client is not None
        started = time.monotonic()

        url, expected = item.url, item.size
        if item.meta.get("composite"):
            # Cloud documents are produced by an async job; resolve it now so
            # the presigned link is fresh (they expire in about three hours)
            # and so the size we verify against is the job's, not the
            # listing's — the two differ.
            from adobe_rescue import platform
            url, job_size = platform.resolve_composite(
                self.client, item.url, item.api_key or "", log=LOG)
            expected = job_size or item.size

        big = (expected or item.size) >= 20 * 1024 * 1024
        beat = {"at": time.monotonic()}

        def on_chunk(done: int, total: int) -> None:
            self._report_bytes(item.key, done)
            now = time.monotonic()
            if big and now - beat["at"] >= 15:
                beat["at"] = now
                LOG.info("    … %s %s / %s", item.name,
                         human_size(done), human_size(total or expected or item.size))

        try:
            size = self.client.download(url, target, api_key=item.api_key,
                                        expected_size=expected,
                                        on_progress=on_chunk)
        finally:
            with self._lock:
                self._inflight.pop(item.key, None)
        elapsed = max(time.monotonic() - started, 0.001)
        LOG.debug("  ✓ %s (%s, %.1f KB/s)", item.name, human_size(size),
                  size / elapsed / 1024)
        return size

    def close(self) -> None:
        if self.client:
            self.client.close()
