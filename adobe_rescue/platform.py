"""Adobe Cloud Platform repository helpers.

The storage API is hypermedia: `GET /index` returns the account's
repositories and a set of `_links` whose hrefs are RFC 6570 templates. You
are meant to follow those links, not build paths — which is why every
hand-written URL in earlier versions returned 400 or 404.

Nothing here knows a path by heart beyond the one documented entry point.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import quote

INDEX_URL = "https://platform-cs.adobe.io/index"

#: Link relations are long ns URLs; we match on the meaningful suffix.
PAGE_RELS = ("rel/page", "rel/directory", "rel/children", "rel/contents")
DOWNLOAD_RELS = ("rel/download", "rel/primary", "rel/rendition", "rel/content")
RESOLVE_PATH_RELS = ("rel/resolve/path",)
RESOLVE_ID_RELS = ("rel/resolve/id",)

_TEMPLATE = re.compile(r"\{\?([^}]+)\}")


_PATH_TEMPLATE = re.compile(r"/[^/{}]*\{[^}]+\}[^/{}]*")


def expand(template: str, **params: Any) -> str:
    """Expand Adobe's link templates.

    Two forms appear: the `{?a,b,c}` query form, and path segments such as
    `/resource:{resource}/`. Unsupplied path segments are dropped — the live
    API answers 4001 "Resource=... is not supported" when they are guessed
    at, and 200 when they are simply absent.
    """
    match = _TEMPLATE.search(template)
    query = ""
    if match:
        names = [n.strip() for n in match.group(1).split(",")]
        pairs = [f"{n}={quote(str(params[n]), safe='')}"
                 for n in names if params.get(n) not in (None, "")]
        query = ("?" + "&".join(pairs)) if pairs else ""
        template = template[:match.start()] + template[match.end():]

    head, sep, tail = template.partition("?")
    head = _PATH_TEMPLATE.sub(lambda m: _fill_segment(m.group(0), params), head)
    return head + sep + tail + query


def _fill_segment(segment: str, params: Dict[str, Any]) -> str:
    """Substitute a path segment's variable, or drop the segment."""
    name = segment[segment.index("{") + 1: segment.index("}")].lstrip("+#./;?&")
    value = params.get(name)
    if value in (None, ""):
        return ""
    return segment[:segment.index("{")] + quote(str(value), safe="") + \
        segment[segment.index("}") + 1:]


def links_of(node: Dict[str, Any]) -> Dict[str, Any]:
    """Links of a node, wherever Adobe put them.

    On the index, a repository's `_links` sit inside its `_embedded`
    metadata object rather than on the child itself; on page responses the
    children carry `_links` directly. Merge both, preferring the node's own.
    """
    merged: Dict[str, Any] = {}
    emb = node.get("_embedded")
    if isinstance(emb, dict):
        for value in emb.values():
            for candidate in (value if isinstance(value, list) else [value]):
                if isinstance(candidate, dict) and isinstance(candidate.get("_links"), dict):
                    merged.update(candidate["_links"])
    own = node.get("_links")
    if isinstance(own, dict):
        merged.update(own)
    return merged


def pick_link(links: Dict[str, Any], suffixes: Iterable[str],
              mode: str = "id") -> str:
    """First href whose relation ends with one of `suffixes`.

    A relation's value is often a LIST of variants distinguished by `mode`
    ("id" or "path"); we prefer the id form, which needs no path encoding.
    """
    for suffix in suffixes:
        for rel, value in links.items():
            if not str(rel).rstrip("/").endswith(suffix):
                continue
            if isinstance(value, dict) and value.get("href"):
                return str(value["href"])
            if isinstance(value, list):
                options = [v for v in value if isinstance(v, dict) and v.get("href")]
                if not options:
                    continue
                for option in options:
                    if str(option.get("mode", "")).lower() == mode:
                        return str(option["href"])
                return str(options[0]["href"])
    return ""


def embedded(node: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten `_embedded`, whose keys are also long relation URLs."""
    data = node.get("_embedded")
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for value in data.values():
        if isinstance(value, dict):
            out.update(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    out.update(item)
    return out


def field(node: Dict[str, Any], *names: str) -> Any:
    """Read a field that may be plain, namespaced, or inside `_embedded`."""
    merged = {**embedded(node), **node}
    for name in names:
        for key in (name, f"repo:{name}", f"storage:{name}", f"dc:{name}"):
            if merged.get(key) not in (None, ""):
                return merged[key]
    return None


def is_directory(node: Dict[str, Any]) -> bool:
    asset_class = str(field(node, "assetClass", "assetclass") or "")
    if asset_class:
        # "composite" is a cloud document or library: an asset, not a folder.
        return asset_class.lower() == "directory"
    node_type = str(field(node, "type") or "")
    return "directory" in node_type.lower()


def node_id(node: Dict[str, Any]) -> str:
    for name in ("assetId", "repositoryId", "id", "urn"):
        value = field(node, name)
        if value:
            return str(value)
    return ""


def node_name(node: Dict[str, Any]) -> str:
    for name in ("name", "path", "title"):
        value = field(node, name)
        if value:
            return str(value).rstrip("/").rsplit("/", 1)[-1] or str(value)
    return node_id(node) or "untitled"


def children_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("children", "items", "entries", "resources"):
        value = payload.get(key)
        if isinstance(value, list):
            return [c for c in value if isinstance(c, dict)]
    emb = payload.get("_embedded")
    if isinstance(emb, dict):
        for value in emb.values():
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
    return []


def next_page(payload: Dict[str, Any]) -> str:
    links = links_of(payload)
    for rel, value in links.items():
        if str(rel).rstrip("/").endswith("rel/next") or rel == "next":
            if isinstance(value, dict) and value.get("href"):
                return str(value["href"])
    return ""


def repositories(index: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """(repositoryId, node) for every repository the index advertises."""
    out = []
    for child in children_of(index):
        repo_id = str(field(child, "repositoryId") or node_id(child) or "")
        if repo_id:
            out.append((repo_id, child))
    return out


def listing_url(index: Dict[str, Any], repo_id: str, node: Dict[str, Any],
                path: str = "/") -> str:
    """Best available way to list a repository root.

    Prefers a link the server gave us; falls back to the resolve template
    from the index, which is the documented entry point.
    """
    direct = pick_link(links_of(node), PAGE_RELS)
    if direct:
        return expand(direct, limit=100)
    template = pick_link(links_of(index), RESOLVE_PATH_RELS)
    if template:
        return expand(template, repositoryId=repo_id, path=path, resource="page")
    template = pick_link(links_of(index), RESOLVE_ID_RELS)
    if template:
        return expand(template, repositoryId=repo_id, id=repo_id, resource="page")
    return ""


def walk(client, index_url: str, api_key: str, log=None):
    """Yield (path_parts, node) for every asset in every repository.

    Follows the server's own links throughout: the index names the
    repositories, each repository hands back a listing link, and each
    directory carries its own page link.
    """
    index = client.get_json(index_url, api_key=api_key)
    repos = repositories(index)
    if log:
        log.info("  %d depo bulundu", len(repos))

    seen_urls: set = set()

    def descend(url: str, parts: List[str], depth: int):
        url = expand(url, limit=100)
        if not url or depth > 24 or url in seen_urls:
            return
        seen_urls.add(url)
        page_url = url
        while page_url:
            payload = client.get_json(page_url, api_key=api_key)
            for child in children_of(payload):
                name = node_name(child)
                if is_directory(child):
                    href = pick_link(links_of(child), PAGE_RELS)
                    if href:
                        # Children's page links are templated too; descend()
                        # expands, but be explicit so this cannot regress.
                        yield from descend(expand(href, limit=100),
                                           parts + [name], depth + 1)
                    continue
                yield parts, child
            page_url = next_page(payload)

    for repo_id, node in repos:
        start = listing_url(index, repo_id, node)
        if not start:
            if log:
                log.warning("  ! %s için listeleme bağlantısı yok", repo_id[:48])
            continue
        yield from descend(start, [], 0)


# --------------------------------------------------------------------------
# Composite (cloud document) downloads
#
# A composite is not fetched directly. The download link starts a job:
#   1. GET <download href>            -> 202 + Location + Retry-After
#   2. poll Location                  -> 202 ... then 200
#   3. the 200 is content-type application/http: a raw embedded HTTP
#      response whose body, after the first blank line, is JSON holding a
#      presigned S3 href and the authoritative size
#   4. GET that href WITHOUT credentials
# Verified against a live account; every step here was observed, not guessed.
# --------------------------------------------------------------------------
COMPOSITE_TIMEOUT = 300.0
MAX_RETRY_AFTER = 15.0


def parse_embedded_http(data: bytes) -> Dict[str, Any]:
    """Pull the JSON body out of an `application/http` payload."""
    import json as _json

    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    for separator in (b"\r\n\r\n", b"\n\n"):
        head, found, body = data.partition(separator)
        if found and body.strip().startswith(b"{"):
            return _json.loads(body.decode("utf-8", "replace"))
    text = data.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        return _json.loads(text)
    raise ValueError("beklenmeyen composite yanıtı")


def resolve_composite(client, url: str, api_key: str, log=None) -> Tuple[str, int]:
    """Run the download job and return (presigned href, size in bytes).

    Called immediately before fetching, so the presigned link is fresh —
    they expire in roughly three hours.
    """
    import time as _time

    from adobe_rescue.http import DownloadError

    # Supplying ?resource= here returns 400; the bare URL is what works.
    url = expand(url)
    response = client.request("GET", url, api_key=api_key)
    deadline = _time.monotonic() + COMPOSITE_TIMEOUT

    while response.status_code == 202:
        location = response.headers.get("Location")
        retry = response.headers.get("Retry-After") or "1"
        try:
            delay = min(float(retry), MAX_RETRY_AFTER)
        except ValueError:
            delay = 1.0
        response.close()
        if not location:
            raise DownloadError("composite işi Location başlığı döndürmedi")
        if _time.monotonic() > deadline:
            raise DownloadError("composite işi zaman aşımına uğradı")
        _time.sleep(max(delay, 0.2))
        response = client.request("GET", location, api_key=api_key)

    if response.status_code != 200:
        status = response.status_code
        response.close()
        raise DownloadError(f"composite işi başarısız (HTTP {status})")

    raw = response.content
    response.close()
    try:
        payload = parse_embedded_http(raw)
    except ValueError as exc:
        raise DownloadError(f"composite yanıtı çözülemedi: {exc}") from exc

    href = str(payload.get("href") or "")
    if not href:
        raise DownloadError("composite yanıtında indirme adresi yok")
    if log:
        log.debug("composite hazır (%s bayt)", payload.get("size"))
    return href, int(payload.get("size") or 0)
