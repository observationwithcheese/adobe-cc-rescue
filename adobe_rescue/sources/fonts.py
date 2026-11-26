"""Adobe Fonts.

Activated fonts do not transfer to a new account; the announcement asks for a
written list so they can be re-activated. We recover that list from the local
CoreSync 'livetype' database (works offline, no token) and, when a session is
available, cross-check it against the web list.

Output: `fonts/aktif-fontlar.txt` — one family/style per line, plus a CSV.
"""

from __future__ import annotations

import csv
import re
from typing import Iterator, Set

from adobe_rescue import config
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG

FONT_NAME_RE = re.compile(rb'"(?:family|fullName|postscriptName|name)"\s*:\s*"([^"]{2,80})"')


class Fonts(Source):
    name = "fonts"
    title = "Aktif Adobe Fonts listesi"
    needs_auth = False
    empty_hint = ("Aktif font bulunamadı. fonts.adobe.com/my_fonts sayfasını açıp listeyi "
                  "ekran görüntüsüyle de saklayın.")

    def discover(self, ctx: Context) -> Iterator[Item]:
        names = _from_coresync()
        source_label = "CoreSync (yerel)"

        if ctx.client is not None:
            try:
                web = _from_web(ctx)
                if web:
                    names |= web
                    source_label = "CoreSync + web"
            except Exception as exc:
                # Deliberately swallowed, AuthError included: the local CoreSync
                # list is still worth writing, and the cloud sources will report
                # an expired token loudly enough on their own.
                LOG.debug("Web font listesi alınamadı: %s", exc)

        if not names:
            LOG.warning(self.empty_hint)
            return

        out_dir = ctx.source_dir(self.name)
        txt = out_dir / "aktif-fontlar.txt"
        csv_path = out_dir / "aktif-fontlar.csv"
        ordered = sorted(names, key=lambda s: s.lower())

        txt.write_text(
            "Aktif Adobe Fonts (kaynak: %s)\n%s\n\n%s\n" % (
                source_label, "=" * 48, "\n".join(ordered)),
            encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["font", "yeni hesapta etkinleştirildi mi"])
            for name in ordered:
                writer.writerow([name, ""])

        LOG.info("  %d font kaydedildi → %s", len(ordered), txt.name)
        yield Item(
            key=f"{self.name}:list",
            name=txt.name,
            parts=[],
            size=txt.stat().st_size,
            local_path=txt,
            note=f"{len(ordered)} font. Yeni hesapta fonts.adobe.com üzerinden tekrar etkinleştirin.",
        )


def _from_coresync() -> Set[str]:
    names: Set[str] = set()
    for base in config.font_cache_dirs():
        if not base.exists():
            continue
        LOG.info("  Font veritabanı: %s", base)
        # The activated faces themselves are the most reliable source: Adobe
        # names the files opaquely, but each font carries its own name table.
        from adobe_rescue.fontnames import names_in_tree
        from_files = names_in_tree(base)
        if from_files:
            LOG.info("  %d font dosyadan okundu", len(from_files))
            names |= from_files
        for file in list(base.rglob("*"))[:5000]:
            if not file.is_file():
                continue
            if file.suffix.lower() in {".json", ".db", ".plist", ".xml", ""}:
                try:
                    blob = file.read_bytes()[: 4 * 1024 * 1024]
                except OSError:
                    continue
                for match in FONT_NAME_RE.findall(blob):
                    try:
                        value = match.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if _plausible(value):
                        names.add(value)
    return names


def _from_web(ctx: Context) -> Set[str]:
    client = ctx.client
    assert client is not None
    url = client.adobe.endpoint("fonts", "sync")
    api_key = client.adobe.endpoint("fonts", "api_key")
    data = client.get_json(url, api_key=api_key)
    found: Set[str] = set()
    for key in ("fonts", "families", "items", "sync_fonts"):
        for node in (data.get(key) or []) if isinstance(data, dict) else []:
            if isinstance(node, str):
                found.add(node)
            elif isinstance(node, dict):
                value = node.get("family") or node.get("name") or node.get("full_name")
                if value and _plausible(str(value)):
                    found.add(str(value))
    return found


def _plausible(value: str) -> bool:
    if len(value) < 3 or len(value) > 80:
        return False
    if value.startswith(("http", "{", "/", "com.")):
        return False
    return bool(re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", value))
