"""CC Libraries.

A library is only portable as a whole `.cclibs` export, and that export is
only exposed in the Creative Cloud desktop app — there is no API for it. So
this source *inventories* the libraries (name, element counts, id) and emits
one manual checklist entry per library, plus a per-library JSON inventory you
can diff against the new account afterwards.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

from adobe_rescue.auth import AuthError
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG, safe_name


class Libraries(Source):
    name = "libraries"
    title = "Kütüphaneler (.cclibs)"
    needs_auth = True
    access_denied_hint = (
        "Kütüphane paketleriniz zaten indirildi: cloud-files\\assets\\adobe-libraries klasörüne bakın. Ancak bu dosyalar doğrudan içe aktarılamaz. Yeni hesaba taşımak için Creative Cloud masaüstü uygulaması > Files > Your libraries > kütüphanenin üzerine gelin > Export ile .cclibs olarak kaydedin.")
    empty_hint = "Kütüphane bulunamadı."

    def discover(self, ctx: Context) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        url = client.adobe.endpoint("libraries", "list")
        api_key = client.adobe.endpoint("libraries", "api_key")

        try:
            data = client.get_json(url, api_key=api_key, params={"limit": 200})
        except AuthError:
            raise
        except Exception as exc:
            LOG.warning("Kütüphane listesi alınamadı: %s", exc)
            yield Item(
                key=f"{self.name}:manual-all",
                name="TUM-KUTUPHANELER.cclibs",
                manual=True,
                note=("Kütüphane listesi API'den alınamadı. Creative Cloud masaüstü "
                      "uygulamasında Files > Your libraries > kütüphane üzerinde Export ile "
                      "her kütüphaneyi tek tek .cclibs olarak dışa aktarın."),
            )
            return

        libraries: List[Dict[str, Any]] = (
            data.get("libraries") or data.get("items") or data.get("children") or []
        )
        if not libraries:
            return

        out_dir = ctx.source_dir(self.name)
        inventory = []
        for lib in libraries:
            lib_id = str(lib.get("id") or lib.get("library_id") or "")
            lib_name = str(lib.get("name") or lib.get("displayName") or lib_id or "library")
            elements = lib.get("elements") or lib.get("totalElements") or lib.get("element_count")
            count = len(elements) if isinstance(elements, list) else int(elements or 0)
            inventory.append({"id": lib_id, "name": lib_name, "elements": count,
                              "modified": lib.get("modified", "")})
            yield Item(
                key=f"{self.name}:{lib_id or lib_name}",
                name=f"{safe_name(lib_name)}.cclibs",
                size=0,
                manual=True,
                note=(f"'{lib_name}' ({count} öğe): Creative Cloud masaüstü uygulaması > "
                      f"Files > Your libraries > kütüphanenin üzerine gel > Export > "
                      f"{safe_name(lib_name)}.cclibs olarak bu klasöre kaydet. "
                      f"Renk, font ve şablonlar tek tek dışa aktarılamaz; kütüphaneyi bütün olarak alın."),
                meta={"id": lib_id, "elements": count},
            )

        try:
            (out_dir / "kutuphane-envanteri.json").write_text(
                json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
            LOG.info("  %d kütüphane envantere yazıldı.", len(inventory))
        except OSError as exc:
            LOG.warning("Kütüphane envanteri yazılamadı: %s", exc)
