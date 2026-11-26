"""Creative Cloud Files (assets.adobe.com / adobe.com/files).

Walks the whole asset tree breadth-first and yields every regular file.
Cloud documents (.psdc/.aic/.xd) are handed to cloud_docs.py instead, because
they need an export step before they are usable outside Adobe's apps.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

from adobe_rescue.auth import AuthError
from adobe_rescue import platform
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG

CLOUD_DOC_EXTS = (".psdc", ".aic", ".idc", ".xd", ".psdcloud", ".aicloud")
DIR_TYPES = ("application/vnd.adobe.directory+json", "directory")


def _is_dir(node: Dict[str, Any]) -> bool:
    return (node.get("type") in DIR_TYPES
            or node.get("_type") == "directory"
            or str(node.get("path", "")).endswith("/"))


def _node_id(node: Dict[str, Any]) -> str:
    for key in ("id", "asset_id", "assetId", "repo:assetId"):
        if node.get(key):
            return str(node[key])
    links = node.get("_links") or {}
    self_link = links.get("self") or {}
    href = self_link.get("href", "") if isinstance(self_link, dict) else ""
    return href.rstrip("/").rsplit("/", 1)[-1]


def _download_href(node: Dict[str, Any]) -> str:
    links = node.get("_links") or {}
    for key in ("http://ns.adobe.com/adobecloud/rel/download",
                "download", "primary", "self"):
        link = links.get(key)
        if isinstance(link, dict) and link.get("href"):
            return str(link["href"])
        if isinstance(link, list) and link and isinstance(link[0], dict):
            return str(link[0].get("href", ""))
    return ""


class CloudFiles(Source):
    name = "cloud-files"
    title = "Creative Cloud Dosyaları"
    needs_auth = True
    empty_hint = ("Bulutta dosya görünmüyor. Creative Cloud masaüstü uygulamasında "
                  "Files sekmesinin senkronu tamamladığından emin olun.")

    def discover(self, ctx: Context) -> Iterator[Item]:
        """Walk the platform-cs repositories, then the legacy /files folder."""
        client = ctx.client
        assert client is not None
        seen: set = set()
        for item in self._walk_platform(ctx, seen):
            yield item
        for item in self._walk_legacy(ctx, seen):
            yield item

    # ---------------------------------------------------------- platform-cs
    def _walk_platform(self, ctx: Context, seen: set) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        api_key = client.adobe.endpoint("cloud_files", "api_key")
        index_url = client.adobe.endpoint("cloud_files", "index")
        count = 0
        try:
            for parts, node in platform.walk(client, index_url, api_key, log=LOG):
                name = platform.node_name(node)
                if name.lower().endswith(CLOUD_DOC_EXTS):
                    continue            # cloud-docs source handles these
                asset_id = platform.node_id(node)
                if asset_id and asset_id in seen:
                    continue
                href = platform.pick_link(platform.links_of(node), platform.DOWNLOAD_RELS)
                if not href:
                    continue
                href = platform.expand(href)
                if asset_id:
                    seen.add(asset_id)
                count += 1
                yield Item(
                    key=f"{self.name}:{asset_id or href}",
                    name=name,
                    parts=parts,
                    size=int(platform.field(node, "size") or 0),
                    url=href,
                    api_key=api_key,
                    modified=str(platform.field(node, "modifyDate", "modified") or ""),
                    meta={"id": asset_id,
                          "composite": str(platform.field(node, "assetClass") or
                                           "").lower() == "composite"},
                )
        except AuthError:
            raise
        except Exception as exc:
            LOG.warning("  ! depo taranamadı: %s", exc)
        if count:
            LOG.info("  %d dosya bulundu", count)

    # -------------------------------------------------------------- legacy
    def _walk_legacy(self, ctx: Context, seen: set) -> Iterator[Item]:
        """The old Creative Cloud Files folder; empty on newer accounts."""
        client = ctx.client
        assert client is not None
        api_key = client.adobe.endpoint("cloud_files", "api_key")
        url = client.adobe.endpoint("cloud_files", "list_root")

        queue: List[tuple] = [(url, [])]
        folders = 0
        while queue:
            current, parts = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            folders += 1
            if folders % 25 == 0:
                LOG.info("  … %d klasör tarandı", folders)
            try:
                children = list(client.paginate(current, api_key=api_key,
                                                params={"limit": 100}))
            except AuthError:
                raise
            except Exception as exc:
                LOG.debug("eski klasör atlandı %s (%s)", "/".join(parts) or "/", exc)
                continue

            for node in children:
                name = str(node.get("name") or node.get("repo:name") or _node_id(node))
                if _is_dir(node):
                    href = _download_href(node) or node.get("_links", {}).get("self", {}).get("href", "")
                    if href:
                        queue.append((href, parts + [name]))
                    continue
                if name.lower().endswith(CLOUD_DOC_EXTS):
                    continue
                href = _download_href(node)
                asset_id = _node_id(node)
                if not href or (asset_id and asset_id in seen):
                    continue
                if asset_id:
                    seen.add(asset_id)
                yield Item(
                    key=f"{self.name}:{asset_id}",
                    name=name,
                    parts=parts,
                    size=int(node.get("size") or node.get("repo:size") or 0),
                    url=href,
                    api_key=api_key,
                    modified=str(node.get("modified") or node.get("repo:modifyDate") or ""),
                    meta={"id": asset_id},
                )
