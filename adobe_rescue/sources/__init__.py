"""Source registry."""

from __future__ import annotations

from typing import Dict, List, Type

from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.sources.acrobat import Acrobat
from adobe_rescue.sources.cloud_docs import CloudDocs
from adobe_rescue.sources.cloud_files import CloudFiles
from adobe_rescue.sources.express import Express
from adobe_rescue.sources.fonts import Fonts
from adobe_rescue.sources.libraries import Libraries
from adobe_rescue.sources.lightroom import Lightroom
from adobe_rescue.sources.local_sync import LocalExtras, LocalSync
from adobe_rescue.sources.presets import Presets

#: Execution order matters: cheap local sources first so that an expiring
#: token never costs you the offline material.
REGISTRY: List[Type[Source]] = [
    LocalSync,
    Presets,
    Fonts,
    LocalExtras,
    CloudFiles,
    CloudDocs,
    Acrobat,
    Lightroom,
    Libraries,
    Express,
]

ALL_NAMES: List[str] = [cls.name for cls in REGISTRY]
BY_NAME: Dict[str, Type[Source]] = {cls.name: cls for cls in REGISTRY}
LOCAL_NAMES: List[str] = [cls.name for cls in REGISTRY if not cls.needs_auth]
REMOTE_NAMES: List[str] = [cls.name for cls in REGISTRY if cls.needs_auth]


def build(names: List[str]) -> List[Source]:
    out: List[Source] = []
    for name in names:
        cls = BY_NAME.get(name)
        if cls is None:
            raise KeyError(f"Bilinmeyen kaynak: {name}. Seçenekler: {', '.join(ALL_NAMES)}")
        out.append(cls())
    return out


__all__ = ["Context", "Item", "Source", "REGISTRY", "ALL_NAMES", "BY_NAME",
           "LOCAL_NAMES", "REMOTE_NAMES", "build"]
