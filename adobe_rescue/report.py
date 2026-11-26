"""Human-readable output: an HTML report and a TODO checklist.

The report is the thing you actually hand back to the admin: what came down,
what did not, and the exact manual steps still owed before the cut-off.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from adobe_rescue import __version__, config
from adobe_rescue.engine import Result
from adobe_rescue.manifest import (Manifest, STATUS_DONE, STATUS_FAILED,
                                   STATUS_MANUAL, STATUS_SKIPPED)
from adobe_rescue.util import human_delta, human_size, now_stamp

SOURCE_TITLES = {
    "local-sync": "Yerel Creative Cloud Files",
    "local-extras": "Lightroom Classic katalogları",
    "presets": "Ön ayarlar (fırça / aksiyon / renk)",
    "fonts": "Adobe Fonts listesi",
    "cloud-files": "Bulut dosyaları",
    "cloud-docs": "Bulut belgeleri (.psdc/.aic)",
    "acrobat": "Acrobat belgeleri",
    "lightroom": "Lightroom (bulut)",
    "libraries": "Kütüphaneler (.cclibs)",
    "express": "Adobe Express",
}

CSS = """
:root{color-scheme:light dark;--bg:#faf9f7;--fg:#1c1b19;--muted:#6b6862;
--card:#fff;--line:#e5e2dc;--ok:#2f7d4f;--warn:#b7791f;--bad:#c0392b;--accent:#1473e6}
@media(prefers-color-scheme:dark){:root{--bg:#17161a;--fg:#ece9e4;--muted:#9a958d;
--card:#201f24;--line:#32303a}}
*{box-sizing:border-box}body{margin:0;padding:2rem 1.25rem;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto}h1{font-size:1.6rem;margin:0 0 .25rem}
.sub{color:var(--muted);margin:0 0 1.5rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1.5rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
.card b{display:block;font-size:1.5rem;font-weight:650}
.card span{color:var(--muted);font-size:.82rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;margin-bottom:1.5rem}
th,td{text-align:left;padding:.55rem .75rem;border-bottom:1px solid var(--line);font-size:.9rem}
th{background:rgba(127,127,127,.08);font-weight:600}tr:last-child td{border-bottom:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
h2{font-size:1.1rem;margin:2rem 0 .6rem}
li{margin-bottom:.5rem}code{background:rgba(127,127,127,.14);padding:.1rem .35rem;border-radius:4px}
.deadline{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
border-radius:8px;padding:.8rem 1rem;margin-bottom:1.5rem}
.scroll{overflow-x:auto}
"""


def _clean(note: str) -> str:
    """Strip Adobe's raw JSON out of a note shown to a person."""
    text = (note or "").strip()
    for marker in ("while (1)", "{\""):
        index = text.find(marker)
        if index > 0:
            text = text[:index].rstrip(" .:;—-")
    return text


def _psdc_warning(manifest: Manifest) -> str:
    """Cloud documents need a banner, not a footnote.

    Once the PSD conversion has run, the warning is not merely unnecessary —
    it is wrong, and tells someone to redo work that is finished.
    """
    docs = [e for e in manifest.by_source("cloud-docs")
            if e.status == STATUS_DONE and e.name.lower().endswith((".psdc", ".aic"))]
    if not docs:
        return ""

    converted = manifest.get("cloud-docs:__to-psd__")
    if converted is not None and converted.status == STATUS_DONE:
        return (
            '<div class="deadline" style="border-left-color:#2f7d4f">'
            f'<b>Düzenlenebilir PSD: {len(docs)}/{len(docs)} hazır.</b><br>'
            f'{html.escape(converted.note or "")}<br>'
            'Bulut belgeleriniz katmanlı <code>.psd</code> olarak kaydedildi; '
            'Photoshop\'ta normal şekilde açılırlar. İndirilen <code>.psdc</code> '
            'paketleri arşiv kopyası olarak duruyor.'
            '</div>')

    return (
        '<div class="deadline" style="border-left-color:#c0392b">'
        f'<b>Dikkat — {len(docs)} bulut belgesi indirildi, ama bu dosyalar '
        'Photoshop\'ta AÇILAMAZ.</b><br>'
        'Adobe, dışa aktarılmış bulut belgelerinin yerelde açılmasına izin vermiyor '
        '("cloud documents that have been exported as local files cannot be opened"). '
        'İndirilen <code>.psdc</code> dosyaları arşiv kopyasıdır; yanlarındaki '
        '<code>_duz-gorseller</code> klasöründe düz görselleri vardır.<br><br>'
        '<b>Düzenlenebilir kopya için, hesap geçişinden ÖNCE:</b> her belgeyi '
        'Photoshop\'ta buluttan açın → <b>Dosya > Kopyasını kaydet</b> → yerel '
        '<code>.psd</code>. Aşağıdaki listede hepsi işaretlenebilir şekilde duruyor.'
        '</div>')


def _deadline_banner() -> str:
    due = config.deadline()
    if not due:
        return ""
    left = due - datetime.now()
    status = "kaldı" if left.total_seconds() > 0 else "GEÇTİ"
    cls = "ok" if left.total_seconds() > 12 * 3600 else "bad"
    return (f'<div class="deadline"><b>Son tarih:</b> '
            f'{due.strftime("%d.%m.%Y %H:%M")} — '
            f'<span class="{cls}">{human_delta(left)} {status}</span></div>')


def write_html(manifest: Manifest, result: Result, path: Path) -> Path:
    totals = manifest.totals()
    by_source: Dict[str, Dict[str, int]] = {}
    for entry in manifest.entries.values():
        bucket = by_source.setdefault(entry.source, {})
        bucket[entry.status] = bucket.get(entry.status, 0) + 1
        bucket["bytes"] = bucket.get("bytes", 0) + (entry.size if entry.status == STATUS_DONE else 0)

    rows = []
    for source, stats in sorted(by_source.items()):
        rows.append(
            "<tr><td>{title}</td><td class='ok'>{done}</td><td class='warn'>{manual}</td>"
            "<td class='bad'>{failed}</td><td>{skipped}</td><td>{size}</td></tr>".format(
                title=html.escape(SOURCE_TITLES.get(source, source)),
                done=stats.get(STATUS_DONE, 0),
                manual=stats.get(STATUS_MANUAL, 0),
                failed=stats.get(STATUS_FAILED, 0),
                skipped=stats.get(STATUS_SKIPPED, 0),
                size=human_size(stats.get("bytes", 0))))

    manual_items = manifest.by_status(STATUS_MANUAL)
    manual_html = "".join(
        f"<li><b>{html.escape(e.name)}</b> — {html.escape(_clean(e.note))}</li>"
        for e in sorted(manual_items, key=lambda x: (x.source, x.name)))

    failed_items = manifest.by_status(STATUS_FAILED)
    failed_html = "".join(
        f"<li><b>{html.escape(e.name)}</b> <span class='bad'>{html.escape(_clean(e.note))}</span></li>"
        for e in failed_items[:200])

    doc = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Adobe yedek raporu</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Adobe yedek raporu</h1>
<p class="sub">{html.escape(now_stamp())} · {html.escape(config.platform_summary())} ·
adobe-cc-rescue {html.escape(__version__)}</p>
{_psdc_warning(manifest)}
{_deadline_banner()}
<div class="cards">
  <div class="card"><b class="ok">{totals.get(STATUS_DONE, 0)}</b><span>indirildi</span></div>
  <div class="card"><b class="warn">{totals.get(STATUS_MANUAL, 0)}</b><span>elle yapılacak</span></div>
  <div class="card"><b class="bad">{totals.get(STATUS_FAILED, 0)}</b><span>hatalı</span></div>
  <div class="card"><b>{human_size(manifest.bytes_done())}</b><span>toplam veri</span></div>
</div>
<div class="scroll"><table>
<tr><th>Kaynak</th><th>İndirildi</th><th>Elle</th><th>Hata</th><th>Atlandı</th><th>Boyut</th></tr>
{''.join(rows) or '<tr><td colspan="6">Kayıt yok</td></tr>'}
</table></div>
<h2>Elle yapılması gerekenler ({len(manual_items)})</h2>
<ol>{manual_html or '<li>Yok — her şey otomatik indirildi.</li>'}</ol>
<h2>Hatalı öğeler ({len(failed_items)})</h2>
<p>Tekrar denemek için: <code>adobe-rescue pull --retry-failed</code></p>
<ul>{failed_html or '<li>Yok.</li>'}</ul>
<h2>Sonraki adımlar</h2>
<ol>
<li>Yukarıdaki "elle yapılacaklar" listesini bitirin (kütüphaneler ve Express kritik).</li>
<li><code>adobe-rescue verify</code> ile sağlama toplamlarını doğrulayın.</li>
<li><code>adobe-rescue package</code> ile tek bir ZIP üretip harici diske/ağ paylaşımına kopyalayın.</li>
<li>Yeni hesap açıldıktan sonra <code>fonts/aktif-fontlar.csv</code> listesindeki fontları tekrar etkinleştirin.</li>
</ol>
</div></body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path


def write_checklist(manifest: Manifest, path: Path) -> Path:
    due = config.deadline()
    lines: List[str] = [
        "# Adobe hesap geçişi — yedek kontrol listesi",
        "",
        f"Oluşturulma: {now_stamp()}",
    ]
    if due:
        left = due - datetime.now()
        lines.append(f"Son tarih: {due.strftime('%d.%m.%Y %H:%M')} ({human_delta(left)} kaldı)")
    docs = [e for e in manifest.by_source("cloud-docs")
            if e.status == STATUS_DONE and e.name.lower().endswith((".psdc", ".aic"))]
    if docs:
        converted = manifest.get("cloud-docs:__to-psd__")
        if converted is not None and converted.status == STATUS_DONE:
            lines += ["", f"## ✓ Bulut belgeleri PSD'ye çevrildi ({len(docs)}/{len(docs)})",
                      "", converted.note or "", ""]
        else:
            lines += [
                "",
                "## ⚠ Bulut belgeleri Photoshop'ta açılamaz",
                "",
                f"{len(docs)} adet `.psdc` indirildi, ama Adobe dışa aktarılmış bulut "
                "belgelerinin yerelde açılmasına izin vermiyor. Bunlar arşiv kopyasıdır.",
                "",
                "**Geçişten ÖNCE** uygulamadaki \"Bulut belgelerini PSD'ye çevir\" "
                "düğmesine basın; hepsi otomatik olarak katmanlı `.psd` olur.",
                "",
            ]
    lines += ["", "## Otomatik indirilenler", ""]
    for source, entries in _group(manifest, STATUS_DONE).items():
        lines.append(f"- [x] {SOURCE_TITLES.get(source, source)} — {len(entries)} öğe")
    lines += ["", "## Elle yapılacaklar", ""]
    manual = _group(manifest, STATUS_MANUAL)
    if not manual:
        lines.append("- [x] Yok.")
    for source, entries in manual.items():
        lines.append(f"### {SOURCE_TITLES.get(source, source)}")
        for entry in sorted(entries, key=lambda e: e.name):
            lines.append(f"- [ ] **{entry.name}** — {_clean(entry.note)}")
        lines.append("")
    failed = _group(manifest, STATUS_FAILED)
    if failed:
        lines += ["## Hatalı — tekrar denenecek", ""]
        for source, entries in failed.items():
            for entry in entries[:100]:
                lines.append(f"- [ ] {entry.name} ({_clean(entry.note)})")
    lines += [
        "",
        "## Geçiş sonrası",
        "",
        "- [ ] Aktif Adobe Fonts listesini yeni hesapta tekrar etkinleştir (`fonts/aktif-fontlar.csv`).",
        "- [ ] Kütüphaneleri yeni hesapta içe aktar. İndirilen "
        "`cloud-files/assets/adobe-libraries/` dosyaları doğrudan içe aktarılamaz; "
        "eski hesap açıkken masaüstü uygulamasından Export ile `.cclibs` alın.",
        "- [ ] Ön ayarları (fırça/aksiyon/renk) ilgili uygulamalarda yükle.",
        "- [ ] Yedeği harici bir diske veya ekip paylaşımına kopyala.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _group(manifest: Manifest, status: str):
    out: Dict[str, list] = {}
    for entry in manifest.entries.values():
        if entry.status == status:
            out.setdefault(entry.source, []).append(entry)
    return out
