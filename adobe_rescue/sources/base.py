"""Source plug-in contract.

A source discovers `Item`s; the engine downloads/copies/records them. Items
come in three flavours:

* remote  — has `url`, fetched over HTTP (cloud files, Acrobat, Lightroom)
* local   — has `local_path`, copied from disk (sync folder, presets, fonts)
* manual  — `manual=True`, cannot be automated; lands in the checklist
            (libraries .cclibs export, Adobe Express documents)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

from adobe_rescue.config import Settings
from adobe_rescue.manifest import Manifest

if TYPE_CHECKING:  # pragma: no cover
    from adobe_rescue.http import Client


@dataclass
class Item:
    key: str
    name: str
    parts: List[str] = field(default_factory=list)   # folders under the source dir
    size: int = 0
    url: str = ""
    local_path: Optional[Path] = None
    api_key: Optional[str] = None
    modified: str = ""
    note: str = ""
    manual: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        return bool(self.url) and not self.manual

    @property
    def is_local(self) -> bool:
        return self.local_path is not None and not self.manual


@dataclass
class Context:
    settings: Settings
    manifest: Manifest
    root: Path
    client: Optional["Client"] = None

    def source_dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class Source:
    """Base class. Subclasses implement `discover`."""

    name = "source"
    title = "Kaynak"
    needs_auth = False
    #: shown in the report when the source finds nothing
    empty_hint = ""
    #: what to do by hand when Adobe refuses this service's client id.
    #: Empty means "no known manual route".
    access_denied_hint = ""

    def discover(self, ctx: Context) -> Iterator[Item]:  # pragma: no cover - abstract
        raise NotImplementedError

    def available(self, ctx: Context) -> bool:
        """Cheap pre-flight. Remote sources need a client; local ones a folder."""
        if self.needs_auth:
            return ctx.client is not None
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Source {self.name}>"
