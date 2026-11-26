"""Lightroom (cloud, not Classic) — original masters via lr.adobe.io.

Lightroom's API wraps every JSON body in a `while (1) {}` guard line, which is
stripped here before parsing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional

from adobe_rescue.auth import AuthError
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG


def _strip_guard(text: str) -> Any:
    cleaned = text.lstrip()
    if cleaned.startswith("while"):
        cleaned = cleaned.split("\n", 1)[-1]
    return json.loads(cleaned)


class Lightroom(Source):
    name = "lightroom"
    title = "Lightroom (bulut)"
    needs_auth = True
    access_denied_hint = (
        "Lightroom (bulut) bu araçla indirilemiyor. lightroom.adobe.com adresine girin > All Photos > tümünü seçin > File > Export > Original + Settings. Lightroom kullanmıyorsanız bu satırı yok sayın.")
    empty_hint = ("Lightroom kataloğu okunamadı. Lightroom masaüstü uygulamasında "
                  "All Photos > tümünü seç > File > Export ile elle dışa aktarın.")

    def _catalog_id(self, ctx: Context) -> Optional[str]:
        client = ctx.client
        assert client is not None
        api_key = client.adobe.endpoint("lightroom", "api_key")
        url = client.adobe.endpoint("lightroom", "catalog")
        try:
            response = client.request("GET", url, api_key=api_key)
            response.raise_for_status()
            data = _strip_guard(response.text)
        except AuthError:
            raise
        except Exception as exc:
            LOG.warning("Lightroom kataloğu alınamadı: %s", exc)
            return None
        return str(data.get("id") or "") or None

    def discover(self, ctx: Context) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        catalog_id = self._catalog_id(ctx)
        if not catalog_id:
            return
        api_key = client.adobe.endpoint("lightroom", "api_key")
        url = client.adobe.endpoint("lightroom", "assets").format(catalog_id=catalog_id)

        cursor: Optional[str] = None
        seen = 0
        while True:
            params: Dict[str, Any] = {"subtype": "image;video", "limit": 100}
            if cursor:
                params["name_after"] = cursor
            try:
                response = client.request("GET", url, api_key=api_key, params=params)
                response.raise_for_status()
                data = _strip_guard(response.text)
            except AuthError:
                raise
            except Exception as exc:
                LOG.warning("Lightroom varlıkları alınamadı: %s", exc)
                return

            resources = data.get("resources") or []
            if not resources:
                return

            for node in resources:
                asset = node.get("asset") or node
                asset_id = str(asset.get("id") or "")
                payload = asset.get("payload") or {}
                imports = payload.get("importSource") or {}
                name = str(imports.get("fileName") or f"{asset_id}.dng")
                captured = str(payload.get("captureDate") or "")
                folder = captured[:7] if len(captured) >= 7 else "tarihsiz"
                links = asset.get("links") or {}
                master = links.get("/rels/master") or links.get("master") or {}
                href = master.get("href") if isinstance(master, dict) else None
                if not href:
                    continue
                full = href if href.startswith("http") else url.split("/catalogs/")[0] + "/" + href.lstrip("/")
                yield Item(
                    key=f"{self.name}:{asset_id}",
                    name=name,
                    parts=[folder],
                    size=int(imports.get("fileSize") or 0),
                    url=full,
                    api_key=api_key,
                    modified=captured,
                    meta={"id": asset_id},
                )
                seen += 1
                cursor = asset_id

            if len(resources) < 100:
                return
            if seen > 100000:  # safety valve
                LOG.warning("Lightroom: 100.000 öğe sınırına ulaşıldı.")
                return
