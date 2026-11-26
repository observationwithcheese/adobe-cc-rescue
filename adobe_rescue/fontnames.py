"""Read family names straight out of OpenType/TrueType files.

Adobe Fonts stores activated faces under CoreSync with opaque numeric file
names, so the filenames tell you nothing. The font's own `name` table does:
this reads name IDs 1 (family) and 4 (full name) without any dependency.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional, Set

FAMILY, FULL_NAME = 1, 4
MAX_HEADER = 1024 * 1024


def font_name(path: Path) -> Optional[str]:
    """Best human name for a font file, or None if it cannot be read."""
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
            if len(head) < 12:
                return None
            tag = head[:4]
            if tag == b"ttcf":                      # collection: first font only
                fh.seek(12)
                offset = struct.unpack(">I", fh.read(4))[0]
                fh.seek(offset)
                head = fh.read(12)
            elif tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
                return None

            num_tables = struct.unpack(">H", head[4:6])[0]
            if not 0 < num_tables < 512:
                return None
            name_offset = name_length = 0
            for _ in range(num_tables):
                record = fh.read(16)
                if len(record) < 16:
                    return None
                if record[:4] == b"name":
                    name_offset, name_length = struct.unpack(">II", record[8:16])
                    break
            if not name_offset or name_length > MAX_HEADER:
                return None

            fh.seek(name_offset)
            table = fh.read(name_length)
    except (OSError, struct.error, ValueError):
        return None

    return _best_name(table)


def _best_name(table: bytes) -> Optional[str]:
    if len(table) < 6:
        return None
    try:
        count, string_offset = struct.unpack(">HH", table[2:6])
    except struct.error:
        return None

    candidates = {}
    for i in range(min(count, 256)):
        base = 6 + i * 12
        if base + 12 > len(table):
            break
        platform, encoding, _lang, name_id, length, offset = struct.unpack(
            ">HHHHHH", table[base:base + 12])
        if name_id not in (FAMILY, FULL_NAME):
            continue
        start = string_offset + offset
        raw = table[start:start + length]
        if not raw:
            continue
        try:
            if platform == 3 or (platform == 0) or encoding == 1 and platform == 0:
                text = raw.decode("utf-16-be", "ignore")
            else:
                text = raw.decode("mac-roman" if platform == 1 else "latin-1", "ignore")
        except (UnicodeDecodeError, LookupError):
            continue
        text = text.strip("\x00 ").strip()
        if text:
            candidates.setdefault(name_id, text)

    return candidates.get(FULL_NAME) or candidates.get(FAMILY)


def names_in_tree(root: Path, limit: int = 4000) -> Set[str]:
    """Every font name found under `root`."""
    found: Set[str] = set()
    if not root.exists():
        return found
    seen = 0
    for path in root.rglob("*"):
        if seen >= limit:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".otf", ".ttf", ".ttc", ".otc", ""):
            continue
        seen += 1
        name = font_name(path)
        if name:
            found.add(name)
    return found
