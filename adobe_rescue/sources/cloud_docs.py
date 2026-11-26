"""Cloud documents (.psdc / .aic / .idc).

These are not flat files — Adobe stores them as a version tree. The web API
can hand back a flattened PSD/AI rendition; when it refuses (common for
InDesign and for very large documents) the item is marked `manual` so it
shows up in the checklist with the exact Photoshop/Illustrator steps from the
announcement ("kopyasını kaydet" → local PSD/AI).
"""

from __future__ import annotations

from typing import Iterator, List

from adobe_rescue.auth import AuthError
from adobe_rescue import platform
from adobe_rescue.sources.base import Context, Item, Source
from adobe_rescue.sources.cloud_files import _download_href, _is_dir, _node_id, CLOUD_DOC_EXTS
from adobe_rescue.util import LOG

#: Verified on Windows: Photoshop refuses an exported cloud document with
#: "cloud documents that have been exported as local files cannot be opened".
#: The package is an archive, not a working file, and the report must say so.
PACKAGE_NOTE = ("Bulut belgesi paketi (arşiv). Photoshop bu dosyayı AÇAMAZ — "
                "düzenlenebilir kopya için belgeyi Photoshop'ta açıp "
                "Dosya > Kopyasını kaydet ile .psd olarak kaydedin.")

EXPORT_FORMAT = {
    ".psdc": "psd",
    ".psdcloud": "psd",
    ".aic": "ai",
    ".aicloud": "ai",
    ".idc": "indd",
    ".xd": "xd",
}


class CloudDocs(Source):
    name = "cloud-docs"
    title = "Bulut Belgeleri (.psdc / .aic)"
    needs_auth = True
    empty_hint = "Bulut belgesi bulunamadı."

    def discover(self, ctx: Context) -> Iterator[Item]:
        client = ctx.client
        assert client is not None
        api_key = client.adobe.endpoint("cloud_docs", "api_key")
        index_url = client.adobe.endpoint("cloud_files", "index")

        found = 0
        converted = 0
        try:
            walker = platform.walk(client, index_url, api_key, log=LOG)
            for parts, node in walker:
                name = platform.node_name(node)
                lower = name.lower()
                if not lower.endswith(CLOUD_DOC_EXTS):
                    continue
                found += 1
                asset_id = platform.node_id(node)
                ext = "." + lower.rsplit(".", 1)[-1]
                fmt = EXPORT_FORMAT.get(ext, "psd")

                href = platform.pick_link(platform.links_of(node), ("rel/download",))
                if href:
                    href = platform.expand(href)
                    # A cloud document is a composite: what comes down is the
                    # DCX package, not a flattened PSD. Keep the original
                    # extension so nobody mistakes it for a plain Photoshop
                    # file, and say what to do with it.
                    yield Item(
                        key=f"{self.name}:{asset_id or href}",
                        name=name, parts=parts,
                        size=int(platform.field(node, "size") or 0),
                        url=href, api_key=api_key,
                        modified=str(platform.field(node, "modifyDate", "modified") or ""),
                        note=PACKAGE_NOTE,
                        meta={"id": asset_id, "original": name, "format": fmt,
                              "composite": True},
                    )

                    # The package cannot be reopened, so also keep a flat
                    # preview: it is the only part of this document that is
                    # viewable without Adobe's cloud.
                    preview = platform.pick_link(platform.links_of(node),
                                                 ("rel/rendition",))
                    if preview:
                        # Confirmed live: the server renders only PNG/JPEG, and
                        # type=image/png with a large size returns the document
                        # at its NATIVE resolution (the size is capped there,
                        # not upscaled). Flattened, but full-resolution pixels.
                        yield Item(
                            key=f"{self.name}:{asset_id}:preview",
                            name=f"{name[: -len(ext)]}.png",
                            parts=["_duz-gorseller"] + parts,
                            url=platform.expand(preview, size=8192, type="image/png"),
                            api_key=api_key,
                            note="Düz görsel, tam çözünürlük (katmansız).",
                            meta={"id": asset_id, "optional": True},
                        )

                    converted += 1
                    continue

                # Adobe exposes no download link for this document: the only
                # supported route is the one from the announcement.
                yield Item(
                    key=f"{self.name}:{asset_id or name}",
                    name=name, parts=parts, manual=True,
                    note=(f"'{name}' bulut belgesi doğrudan indirilemiyor. "
                          f"Photoshop/Illustrator'da açın > Dosya > Kopyasını kaydet "
                          f"> yerel {fmt.upper()} olarak bu klasöre kaydedin."),
                    meta={"id": asset_id, "original": name, "format": fmt},
                )
        except AuthError:
            raise
        except Exception as exc:
            LOG.warning("  ! bulut belgeleri taranamadı: %s", exc)

        if found:
            LOG.info("  %d bulut belgesi bulundu", found)

        if converted:
            # One checklist row, not sixty-one: the conversion step handles
            # these automatically, and a per-document list only buries the
            # instruction that matters.
            yield Item(
                key=f"{self.name}:__to-psd__",
                name=f"{converted} bulut belgesini katmanlı PSD'ye çevir",
                manual=True,
                note=(f"İndirilen .psdc paketleri Photoshop'ta AÇILAMAZ (Adobe izin "
                      f"vermiyor). {converted} belgenin düzenlenebilir kopyası için "
                      f"uygulamadaki \"Bulut belgelerini PSD'ye çevir\" düğmesine basın "
                      f"(veya: adobe-rescue psd --out <klasör>). Photoshop 28+ açık ve iş "
                      f"hesabınızla oturum açmış olmalı. Bunu hesap geçişinden ÖNCE yapın; "
                      f"belge listesi manifest.csv içinde."),
                meta={"count": converted},
            )
