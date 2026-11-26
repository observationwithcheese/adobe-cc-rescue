"""Look inside a downloaded cloud document (.psdc) package.

A .psdc is a UCF (zip) container. The question that decides everything:
does it carry the document as a real PSD stream we can simply lift out, or
only as Adobe's internal component format?

Run:  python tools/dcx_inspect.py <file.psdc> [--extract <outdir>]

Prints every entry with size and what it looks like, flags anything with a
PSD/PSB signature (8BPS), and dumps the manifest. With --extract it writes
out any PSD stream it finds, plus the manifest, for inspection.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PSD_MAGIC = b"8BPS"
SIGNATURES: List[Tuple[bytes, str]] = [
    (b"8BPS", "PSD/PSB belgesi"),
    (b"\x89PNG", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"PK\x03\x04", "zip (iç içe)"),
    (b"%PDF", "PDF"),
    (b"<?xml", "XML"),
    (b"{", "JSON?"),
]


def sniff(blob: bytes) -> str:
    for magic, label in SIGNATURES:
        if blob.startswith(magic):
            return label
    if blob[:4].isascii() and blob[:4].isalpha():
        return f"? ({blob[:4].decode('ascii', 'replace')})"
    return "ikili veri"


def scan(path: Path, extract: Optional[Path]) -> int:
    if not zipfile.is_zipfile(path):
        print(f"{path} bir zip/UCF değil — ham baytlara bakın.")
        return 1

    psd_entries: List[str] = []
    with zipfile.ZipFile(path) as zf:
        infos = sorted(zf.infolist(), key=lambda i: -i.file_size)
        print(f"{path.name}: {len(infos)} girdi, "
              f"{path.stat().st_size / 1048576:.1f} MB\n")
        print(f"{'BOYUT':>12}  {'TÜR':<18} AD")
        for info in infos:
            try:
                with zf.open(info) as fh:
                    head = fh.read(16)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                print(f"{info.file_size:>12}  {'OKUNAMADI':<18} {info.filename} ({exc})")
                continue
            kind = sniff(head)
            if head.startswith(PSD_MAGIC):
                psd_entries.append(info.filename)
                kind = ">>> " + kind
            print(f"{info.file_size:>12}  {kind:<18} {info.filename}")

        # The manifest names the components and usually the composite.
        for candidate in ("manifest", "Manifest", "manifest.json"):
            if candidate in zf.namelist():
                print(f"\n--- {candidate} ---")
                raw = zf.read(candidate)
                try:
                    print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False)[:4000])
                except ValueError:
                    print(raw[:2000].decode("utf-8", "replace"))
                break

        # A PSD can also be embedded without starting the entry; scan for it.
        if not psd_entries:
            print("\nGirdi başlangıcında PSD imzası yok; gömülü arıyorum…")
            for info in infos:
                if info.file_size < 1024:
                    continue
                try:
                    blob = zf.read(info)
                except Exception:
                    continue
                index = blob.find(PSD_MAGIC)
                if index >= 0:
                    print(f"  !! {info.filename} içinde {index} ofsetinde 8BPS")
                    psd_entries.append(f"{info.filename}@{index}")

        print("\n" + "=" * 60)
        if psd_entries:
            print(f"SONUÇ: PSD akışı BULUNDU -> {psd_entries}")
        else:
            print("SONUÇ: PSD akışı YOK. Belge Adobe'nin bileşen formatında "
                  "saklanıyor; düz PSD için başka yol gerekiyor.")

        if extract and psd_entries:
            extract.mkdir(parents=True, exist_ok=True)
            for name in psd_entries:
                entry, _, offset = name.partition("@")
                blob = zf.read(entry)
                if offset:
                    blob = blob[int(offset):]
                target = extract / (Path(entry).name or "extracted")
                if not target.suffix:
                    target = target.with_suffix(".psd")
                target.write_bytes(blob)
                print(f"yazıldı: {target} ({len(blob)} bayt)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--extract", help="bulunan PSD akışlarını bu klasöre yaz")
    args = parser.parse_args()
    return scan(Path(args.file), Path(args.extract) if args.extract else None)


if __name__ == "__main__":
    raise SystemExit(main())
