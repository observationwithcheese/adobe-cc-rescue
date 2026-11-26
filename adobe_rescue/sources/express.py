"""Adobe Express.

Express projects cannot be downloaded in their native form — they must be
converted to another file type inside Express first. This source lists the
projects so nothing is forgotten, and writes a clickable HTML index of direct
project links so the manual pass is mechanical rather than archaeological.
"""

from __future__ import annotations

import html
from typing import Any, Dict, Iterator, List

from adobe_rescue.auth import AuthError
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.util import LOG

PROJECT_URL = "https://express.adobe.com/page/design/?projectId={pid}"


class Express(Source):
    name = "express"
    title = "Adobe Express"
    needs_auth = True
    empty_hint = "Express projesi bulunamadı."

    def discover(self, ctx: Context) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        url = client.adobe.endpoint("express", "list")
        api_key = client.adobe.endpoint("express", "api_key")

        try:
            data = client.get_json(url, api_key=api_key, params={"limit": 200})
        except AuthError:
            raise
        except Exception as exc:
            # projects.adobeprojectm.com no longer resolves at all, so a
            # stack of DNS errors in the log is noise, not information.
            LOG.info("  Express listesi API'den alınamıyor; elle adım eklendi.")
            LOG.debug("express endpoint: %s", exc)
            yield Item(
                key=f"{self.name}:manual-all",
                name="express-projeleri",
                manual=True,
                note=("Express proje listesi alınamadı. express.adobe.com > Your stuff "
                      "sayfasındaki her projeyi açıp Share > Download ile PDF/PNG/MP4 "
                      "olarak dışa aktarın."),
            )
            return

        projects: List[Dict[str, Any]] = data.get("projects") or data.get("items") or []
        rows = []
        for project in projects:
            pid = str(project.get("id") or project.get("projectId") or "")
            name = str(project.get("title") or project.get("name") or pid or "proje")
            link = PROJECT_URL.format(pid=pid)
            rows.append((name, link))
            yield Item(
                key=f"{self.name}:{pid or name}",
                name=name,
                manual=True,
                note=(f"Express projesi '{name}': {link} adresinde aç > Share > Download > "
                      f"PDF (baskı) veya PNG/MP4 (dijital) olarak indir. "
                      f"Express dosyaları doğrudan indirilemez."),
                meta={"id": pid, "url": link},
            )

        if rows:
            out = ctx.source_dir(self.name) / "express-projeleri.html"
            body = "\n".join(
                f'<li><a href="{html.escape(link)}">{html.escape(name)}</a></li>'
                for name, link in rows)
            out.write_text(
                "<!doctype html><meta charset='utf-8'>"
                "<title>Express projeleri</title>"
                "<h1>Express projeleri</h1>"
                "<p>Her birini açıp Share &gt; Download ile indirin.</p>"
                f"<ol>{body}</ol>", encoding="utf-8")
            LOG.info("  %d Express projesi listelendi → %s", len(rows), out.name)
