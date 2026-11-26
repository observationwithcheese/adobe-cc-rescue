"""Acrobat / Document Cloud (acrobat.adobe.com)."""

from __future__ import annotations

from typing import Any, Dict, Iterator

from adobe_rescue.auth import AuthError
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG


class Acrobat(Source):
    name = "acrobat"
    title = "Acrobat / Document Cloud"
    needs_auth = True
    access_denied_hint = (
        "Acrobat belgeleri bu araçla indirilemiyor (Adobe izin vermiyor). acrobat.adobe.com adresine girin > Belgelerim > belgeleri seçin > üç nokta > Download. Acrobat'ta belgeniz yoksa bu satırı yok sayın.")
    empty_hint = ("Acrobat'ta belge görünmüyor. acrobat.adobe.com > Belgelerim sayfasını "
                  "bir kez açıp HAR kaydını tekrar alın.")

    def discover(self, ctx: Context) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        url = client.adobe.endpoint("acrobat", "assets")
        api_key = client.adobe.endpoint("acrobat", "api_key")

        try:
            nodes = list(client.paginate(url, api_key=api_key,
                                         params={"limit": 100}, items_key="resources"))
        except AuthError:
            raise
        except Exception as exc:
            LOG.warning("Acrobat listesi alınamadı: %s", exc)
            return

        for node in nodes:
            asset_id = str(node.get("asset_id") or node.get("id") or "")
            if not asset_id:
                continue
            name = str(node.get("name") or node.get("asset_name") or f"{asset_id}.pdf")
            if "." not in name:
                name += ".pdf"
            href = _href(node) or client.adobe.endpoint("acrobat", "download").format(asset_id=asset_id)
            folder = str(node.get("folder_name") or node.get("parent_name") or "")
            yield Item(
                key=f"{self.name}:{asset_id}",
                name=name,
                parts=[folder] if folder else [],
                size=int(node.get("size") or 0),
                url=href,
                api_key=api_key,
                modified=str(node.get("modified_date") or node.get("created_date") or ""),
                meta={"id": asset_id},
            )


def _href(node: Dict[str, Any]) -> str:
    links = node.get("_links") or {}
    for key in ("download", "self", "content"):
        link = links.get(key)
        if isinstance(link, dict) and link.get("href"):
            return str(link["href"])
    return str(node.get("download_uri") or "")
