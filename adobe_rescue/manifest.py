"""Manifest + resume state.

Every item we ever see is recorded. The manifest doubles as the resume
database: re-running `pull` skips anything already marked `done` with a
matching size/checksum, so an interrupted run on Saturday night costs you
nothing.
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from adobe_rescue.util import LOG, human_size

MANIFEST_NAME = "manifest.json"
CSV_NAME = "manifest.csv"

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_MANUAL = "manual"   # needs a human (libraries, Express, cloud docs)


@dataclass
class Entry:
    key: str                      # stable unique id (source:asset_id)
    source: str
    name: str
    path: str = ""                # relative path inside the backup
    size: int = 0
    status: str = STATUS_PENDING
    sha256: str = ""
    modified: str = ""
    note: str = ""
    url: str = ""
    attempts: int = 0
    updated: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def mark(self, status: str, note: str = "") -> "Entry":
        self.status = status
        if note:
            self.note = note
        self.updated = datetime.now().isoformat(timespec="seconds")
        return self


class Manifest:
    """Thread-safe, crash-tolerant record of the backup."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / MANIFEST_NAME
        self.entries: Dict[str, Entry] = {}
        self._lock = threading.Lock()
        self._dirty = 0
        #: A dry run must not touch the real backup folder.
        self.read_only = False
        self.started = datetime.now().isoformat(timespec="seconds")
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Mevcut manifest okunamadı (%s), sıfırdan başlanıyor.", exc)
            return
        self.started = data.get("started", self.started)
        for raw in data.get("entries", []):
            known = {f for f in Entry.__dataclass_fields__}  # type: ignore[attr-defined]
            entry = Entry(**{k: v for k, v in raw.items() if k in known})
            self.entries[entry.key] = entry
        LOG.debug("Manifestten %d kayıt yüklendi.", len(self.entries))

    def save(self, force: bool = False) -> None:
        if self.read_only:
            return
        with self._lock:
            if not force and self._dirty < 25:
                return
            self._dirty = 0
            payload = {
                "tool": "adobe-cc-rescue",
                "started": self.started,
                "updated": datetime.now().isoformat(timespec="seconds"),
                "entries": [asdict(e) for e in self.entries.values()],
            }
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def drop(self, key: str) -> None:
        """Forget an entry — used to clear a stale 'nothing found' marker."""
        with self._lock:
            if self.entries.pop(key, None) is not None:
                self._dirty += 1

    def export_csv(self) -> Path:
        target = self.root / CSV_NAME
        if self.read_only:
            return target
        with target.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["kaynak", "ad", "yol", "boyut", "durum", "sha256", "not"])
            for e in sorted(self.entries.values(), key=lambda x: (x.source, x.name)):
                writer.writerow([e.source, e.name, e.path, e.size, e.status, e.sha256, e.note])
        return target

    # ------------------------------------------------------------ records
    def get(self, key: str) -> Optional[Entry]:
        return self.entries.get(key)

    def add(self, entry: Entry) -> Entry:
        with self._lock:
            existing = self.entries.get(entry.key)
            if existing:
                # Preserve completion state across re-discovery.
                entry.status = existing.status if existing.status == STATUS_DONE else entry.status
                entry.sha256 = existing.sha256 or entry.sha256
                entry.path = existing.path or entry.path
                entry.attempts = existing.attempts
            self.entries[entry.key] = entry
            self._dirty += 1
        return entry

    def update(self, entry: Entry) -> None:
        with self._lock:
            self.entries[entry.key] = entry
            self._dirty += 1
        self.save()

    def is_done(self, key: str) -> bool:
        entry = self.entries.get(key)
        return bool(entry and entry.status == STATUS_DONE)

    # ------------------------------------------------------------ queries
    def by_status(self, status: str) -> List[Entry]:
        return [e for e in self.entries.values() if e.status == status]

    def by_source(self, source: str) -> List[Entry]:
        return [e for e in self.entries.values() if e.source == source]

    def pending(self) -> List[Entry]:
        return [e for e in self.entries.values()
                if e.status in (STATUS_PENDING, STATUS_FAILED)]

    def totals(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def bytes_done(self) -> int:
        return sum(e.size for e in self.entries.values() if e.status == STATUS_DONE)

    def summary_lines(self) -> Iterable[str]:
        totals = self.totals()
        yield (f"Toplam {len(self.entries)} öğe · "
               f"{totals.get(STATUS_DONE, 0)} indirildi · "
               f"{totals.get(STATUS_FAILED, 0)} hatalı · "
               f"{totals.get(STATUS_MANUAL, 0)} elle yapılacak · "
               f"{totals.get(STATUS_SKIPPED, 0)} atlandı")
        yield f"İndirilen veri: {human_size(self.bytes_done())}"
