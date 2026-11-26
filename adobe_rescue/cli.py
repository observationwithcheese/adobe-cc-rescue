"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from adobe_rescue import __version__, config
from adobe_rescue.auth import AuthError, Session, from_har, from_token
from adobe_rescue.config import Settings
from adobe_rescue.engine import Engine, Progress
from adobe_rescue.manifest import (Manifest, STATUS_DONE, STATUS_FAILED,
                                   STATUS_MANUAL)
from adobe_rescue.report import write_checklist, write_html
from adobe_rescue.sources import ALL_NAMES, LOCAL_NAMES, REGISTRY, REMOTE_NAMES
from adobe_rescue.util import (LOG, human_delta, human_size, free_space_gb,
                               open_in_file_manager, setup_logging, sha256_file,
                               zip_dir)

BANNER = r"""
  Adobe CC Rescue {version}
  Hesap gecisi oncesi toplu yedekleme
""".strip("\n")


# ----------------------------------------------------------------- helpers
def _deadline_line() -> str:
    due = config.deadline()
    if not due:
        return ""
    left = due - datetime.now()
    if left.total_seconds() <= 0:
        return "!! Son tarih GEÇTİ — yine de yedek alın, hesap henüz kapanmamış olabilir."
    return f"Son tarih {due.strftime('%d.%m.%Y %H:%M')} — {human_delta(left)} kaldı."


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if getattr(args, "config", None):
        settings = Settings.from_file(Path(args.config))
    if getattr(args, "output", None):
        settings.output = Path(args.output).expanduser()
    for field_name in ("workers", "timeout", "retries", "rate_limit", "max_size_mb"):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(settings, field_name, value)
    settings.dry_run = bool(getattr(args, "dry_run", False))
    settings.overwrite = bool(getattr(args, "overwrite", False))
    settings.resume = not bool(getattr(args, "no_resume", False))
    settings.verify = not bool(getattr(args, "no_verify", False))
    settings.include = list(getattr(args, "include", []) or [])
    settings.exclude = list(getattr(args, "exclude", []) or [])
    settings.verbose = bool(getattr(args, "verbose", False))
    return settings


def _resolve_sources(args: argparse.Namespace, has_session: bool) -> List[str]:
    chosen = getattr(args, "only", None)
    if chosen:
        names = [n.strip() for n in ",".join(chosen).split(",") if n.strip()]
    elif getattr(args, "local_only", False):
        names = list(LOCAL_NAMES)
    else:
        names = list(ALL_NAMES)
    skip = getattr(args, "skip", None)
    if skip:
        drop = {n.strip() for n in ",".join(skip).split(",") if n.strip()}
        names = [n for n in names if n not in drop]
    if not has_session:
        names = [n for n in names if n not in REMOTE_NAMES]
    unknown = [n for n in names if n not in ALL_NAMES]
    if unknown:
        raise SystemExit(f"Bilinmeyen kaynak: {', '.join(unknown)}\nSeçenekler: {', '.join(ALL_NAMES)}")
    return names


def _console_progress(progress: Progress) -> None:
    """One line: files, bytes, speed, ETA.

    A file counter alone makes a slow 145 MB download look like a hang, so
    bytes and speed matter more than the count does.
    """
    if not progress.total:
        return
    expected = progress.bytes_expected
    if expected:
        pct = min(int(progress.bytes_seen * 100 / expected), 100)
        filled = min(24 * progress.bytes_seen // expected, 24)
    else:
        pct = progress.done * 100 // max(progress.total, 1)
        filled = 24 * progress.done // max(progress.total, 1)
    bar = "█" * filled + "·" * (24 - filled)

    tail = ""
    if expected:
        tail = f"  {human_size(progress.bytes_seen)}/{human_size(expected)}"
    if progress.speed > 1024:
        tail += f"  {human_size(progress.speed)}/sn"
        eta = progress.eta_seconds()
        if eta > 1:
            tail += f"  ~{int(eta // 60)}dk" if eta >= 60 else f"  ~{int(eta)}sn"

    name = (progress.item[:30] + "…") if len(progress.item) > 31 else progress.item
    line = (f"  [{bar}] {pct:3d}%  {progress.done}/{progress.total}{tail}  {name}")
    sys.stdout.write("\r" + line.ljust(118)[:118])
    sys.stdout.flush()
    if progress.done >= progress.total:
        sys.stdout.write("\n")


# ---------------------------------------------------------------- commands
def cmd_login(args: argparse.Namespace) -> int:
    from adobe_rescue import browser_login

    if args.browser_worker:
        if not args.out:
            LOG.error("--browser-worker için --out gerekir")
            return 2
        return browser_login.worker_main(Path(args.out))

    if args.clear:
        Session.load().clear()
        LOG.info("Kayıtlı oturum silindi.")
        return 0

    if args.har:
        session = from_har(Path(args.har).expanduser())
    elif args.token:
        session = from_token(args.token)
    else:
        LOG.info("Adobe giriş penceresi açılıyor — normal şekilde oturum açın.")
        try:
            session = browser_login.login_via_subprocess()
        except AuthError as exc:
            LOG.error("%s", exc)
            LOG.info("")
            LOG.info("Elle giriş (gelişmiş):")
            LOG.info(_instructions())
            return 2

    path = session.save()
    who = session.email or session.user_id or "(bilinmiyor)"
    LOG.info("Oturum kaydedildi: %s", path)
    if session.discovered:
        LOG.info("Web uygulamasından öğrenilen ayar: %d", len(session.discovered))

    from adobe_rescue.selftest import check_cloud_access
    ok, message = check_cloud_access(session)
    LOG.info("Bağlantı testi: %s", "BAŞARILI" if ok else "BAŞARISIZ")
    LOG.info("  %s", message)
    LOG.info("Hesap: %s", who)
    if session.expires:
        LOG.info("Jeton geçerlilik: %s", human_delta(
            datetime.fromtimestamp(session.expires) - datetime.now()))
    if session.discovered:
        LOG.info("HAR'dan bulunan uç noktalar: %d", len(session.discovered))
    return 0


def _instructions() -> str:
    return """
Giriş penceresi çalışmazsa, tarayıcınızın oturumunu elle verebilirsiniz.

  1. Chrome/Edge ile https://assets.adobe.com adresine girin ve oturum açın.
  2. F12 (DevTools) > Network sekmesi > sayfayı yenileyin (F5).
  3. Listede adobe.io ile biten bir isteğe tıklayın.
  4. Headers > Request Headers > "authorization: Bearer eyJ..." satırına
     SAĞ TIKLAYIN ve "Copy value" deyin.

     Satırı fareyle seçip kopyalamayın: Chrome uzun başlıkları ekranda "..."
     ile kısaltır, jeton eksik gelir ve hiçbir istek çalışmaz.

Daha sağlamı (uç noktaları da öğrenir):
  DevTools > Network > sağ tık > "Save all as HAR with content" > sonra
  adobe-rescue login --har indirilen.har
""".strip()


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    LOG.info(BANNER.format(version=__version__))
    LOG.info("")
    LOG.info("Sistem        : %s", config.platform_summary())
    LOG.info("Hedef klasör  : %s", settings.output)
    LOG.info("Boş disk      : %.1f GB", free_space_gb(settings.output))
    line = _deadline_line()
    if line:
        LOG.info("Zaman         : %s", line)

    session = Session.load()
    if session.token:
        state = "SÜRESİ DOLMUŞ" if session.is_expired() else "geçerli"
        LOG.info("Oturum        : %s (%s)", state, session.email or session.user_id or "?")
    else:
        LOG.info("Oturum        : yok — `adobe-rescue login` ile ekleyin "
                 "(yalnızca yerel yedek için gerekmez)")

    LOG.info("")
    LOG.info("Kaynaklar:")
    from adobe_rescue.sources import Context  # local import keeps startup light
    from adobe_rescue.manifest import Manifest as _M
    ctx = Context(settings=settings, manifest=_M(settings.output), root=settings.output,
                  client=None)
    for cls in REGISTRY:
        source = cls()
        if source.needs_auth:
            status = "hazır" if session.token and not session.is_expired() else "oturum gerekli"
        else:
            status = "hazır" if source.available(ctx) else "bulunamadı"
        LOG.info("  %-14s %-46s %s", source.name, source.title, status)

    LOG.info("")
    for path in config.creative_cloud_sync_dirs():
        LOG.info("Yerel senkron : %s", path)
    try:
        import requests  # noqa: F401
        LOG.info("requests      : kurulu")
    except ImportError:
        LOG.warning("requests      : EKSİK — `pip install -r requirements.txt`")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    settings.output.mkdir(parents=True, exist_ok=True)
    setup_logging(settings.verbose, settings.output / "adobe-rescue.log")

    LOG.info(BANNER.format(version=__version__))
    line = _deadline_line()
    if line:
        LOG.info("  %s", line)
    LOG.info("  Hedef: %s", settings.output)

    session = Session.load()
    if session.token and session.is_expired():
        LOG.warning("  ! Jetonun süresi dolmuş görünüyor; yine de denenecek.")
    has_session = bool(session.token)
    if not has_session:
        LOG.warning("  ! Oturum yok — yalnızca yerel kaynaklar yedeklenecek.")

    names = _resolve_sources(args, has_session)
    LOG.info("  Kaynaklar: %s", ", ".join(names))

    engine = Engine(settings, session if has_session else None,
                    on_progress=None if settings.verbose else _console_progress)

    if args.retry_failed:
        count = 0
        for entry in engine.manifest.by_status(STATUS_FAILED):
            entry.mark("pending")
            count += 1
        engine.manifest.save(force=True)
        LOG.info("  %d hatalı öğe tekrar kuyruğa alındı.", count)

    aborted = ""
    result = None
    try:
        result = engine.run(names)
    except AuthError as exc:
        aborted = str(exc)
        LOG.error("\n! %s", exc)
    except KeyboardInterrupt:
        aborted = "kullanıcı durdurdu"
        engine.stop()
        LOG.warning("\n! Durduruldu. İlerleme kaydedildi — aynı komutu tekrar "
                    "çalıştırdığınızda kaldığı yerden devam eder.")
    finally:
        engine.close()
        engine.manifest.save(force=True)

    if result is None:
        # Still report: whatever already came down is real and the user needs
        # to see it, plus the checklist of what is still owed.
        from adobe_rescue.engine import Result as _Result
        result = _Result()
        result.errors.append(aborted)

    manifest = engine.manifest

    LOG.info("")
    for line in manifest.summary_lines():
        LOG.info("  %s", line)

    if settings.dry_run:
        LOG.info("  [deneme] hiçbir dosya yazılmadı; rapor üretilmedi.")
        return 0

    manifest.export_csv()
    html_path = write_html(manifest, result, settings.output / "rapor.html")
    checklist = write_checklist(manifest, settings.output / "YAPILACAKLAR.md")
    LOG.info("  Rapor        : %s", html_path)
    LOG.info("  Yapılacaklar : %s", checklist)
    if aborted:
        LOG.warning("  ! Çalışma yarıda kesildi: %s", aborted.splitlines()[0])
        LOG.warning("  İnen dosyalar korundu; aynı komutu tekrar çalıştırın.")
    if result.failed:
        LOG.warning("  %d öğe indirilemedi — `adobe-rescue pull --retry-failed` deneyin.",
                    result.failed)
    manual = len(manifest.by_status(STATUS_MANUAL))
    if manual:
        LOG.warning("  %d öğe elle yapılmalı — YAPILACAKLAR.md dosyasını açın.", manual)
    if args.open:
        open_in_file_manager(settings.output)
    return 0 if not (result.failed or aborted) else 1


def cmd_verify(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    manifest = Manifest(settings.output)
    done = manifest.by_status(STATUS_DONE)
    if not done:
        LOG.warning("Manifestte doğrulanacak kayıt yok: %s", settings.output)
        return 1

    missing, changed, ok = [], [], 0
    for index, entry in enumerate(done, 1):
        target = settings.output / entry.path if entry.path else None
        sys.stdout.write(f"\r  doğrulanıyor {index}/{len(done)}")
        sys.stdout.flush()
        if not target or not target.exists():
            missing.append(entry)
            continue
        if entry.sha256:
            if sha256_file(target) != entry.sha256:
                changed.append(entry)
                continue
        elif target.stat().st_size != entry.size and entry.size:
            changed.append(entry)
            continue
        ok += 1
    sys.stdout.write("\n")

    LOG.info("  Sağlam: %d · Eksik: %d · Bozuk: %d", ok, len(missing), len(changed))
    for entry in (missing + changed)[:40]:
        LOG.warning("  ! %s (%s)", entry.name, entry.path)
    if missing or changed:
        LOG.info("  Düzeltmek için: adobe-rescue pull --retry-failed")
        return 1
    LOG.info("  Yedek bütünlüğü doğrulandı.")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    src = settings.output
    if not src.exists():
        LOG.error("Yedek klasörü yok: %s", src)
        return 1
    target = Path(args.archive).expanduser() if args.archive else src.with_suffix(".zip")
    LOG.info("Arşivleniyor: %s → %s", src, target)
    zip_dir(src, target, compress=not args.store)
    size = target.stat().st_size
    LOG.info("Bitti: %s (%s)", target, human_size(size))
    LOG.info("Bu dosyayı harici diske veya ekip paylaşımına kopyalayın.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    manifest = Manifest(settings.output)
    if not manifest.entries:
        LOG.error("Manifest bulunamadı: %s", settings.output)
        return 1
    from adobe_rescue.engine import Result
    html_path = write_html(manifest, Result(), settings.output / "rapor.html")
    checklist = write_checklist(manifest, settings.output / "YAPILACAKLAR.md")
    manifest.export_csv()
    LOG.info("Rapor: %s", html_path)
    LOG.info("Yapılacaklar: %s", checklist)
    if args.open:
        open_in_file_manager(settings.output)
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """List the Adobe API calls seen in a HAR — URLs and api keys, no secrets.

    This is the safe thing to share when the built-in endpoints stop working:
    it prints what the Creative Cloud web app actually calls, and deliberately
    never prints the Authorization header, cookies, or any response body.
    """
    import json as _json
    from urllib.parse import urlparse
    from adobe_rescue.auth import _is_adobe

    if args.captured:
        from adobe_rescue.browser_login import load_capture
        rows = load_capture()
        if not rows:
            LOG.error("Kayıtlı istek yok. Önce `adobe-rescue login` ile giriş yapın.")
            return 1
        LOG.info("# Giriş sırasında kaydedilen %d Adobe isteği "
                 "(jeton ve çerezler GÖSTERİLMEZ)", len(rows))
        LOG.info("%-30s %-22s %s", "HOST", "X-API-KEY", "YOL")
        for row in rows:
            full = row.get("path", "")
            if row.get("query"):
                full += "?" + row["query"]
            LOG.info("%-30s %-22s %s", row.get("host", ""),
                     row.get("x-api-key", ""), full)
        return 0

    if not args.har:
        LOG.error("--har veya --captured gerekir")
        return 2

    path = Path(args.har).expanduser()
    try:
        raw = _json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        LOG.error("HAR okunamadı: %s", exc)
        return 1

    seen = {}
    for item in raw.get("log", {}).get("entries", []):
        request = item.get("request", {})
        url = request.get("url", "")
        if not url or not _is_adobe(url):
            continue
        parsed = urlparse(url)
        if parsed.path.rsplit(".", 1)[-1].lower() in (
                "js", "css", "png", "jpg", "jpeg", "svg", "woff", "woff2", "gif", "ico"):
            continue
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in request.get("headers", [])}
        status = item.get("response", {}).get("status", "")
        key = (request.get("method", "GET"), parsed.netloc, parsed.path)
        if key in seen:
            continue
        seen[key] = (headers.get("x-api-key", ""), status)

    if not seen:
        LOG.warning("HAR içinde Adobe API çağrısı bulunamadı.")
        return 1

    LOG.info("# %d Adobe API çağrısı (jeton ve çerezler GÖSTERİLMEZ)", len(seen))
    LOG.info("%-6s %-34s %-52s %-22s %s", "METOD", "HOST", "YOL", "X-API-KEY", "DURUM")
    for (method, host, path_), (api_key, status) in sorted(seen.items()):
        LOG.info("%-6s %-34s %-52s %-22s %s", method, host, path_[:52], api_key, status)
    return 0


def cmd_psd(args: argparse.Namespace) -> int:
    """Convert cloud documents to layered PSD via Photoshop."""
    from adobe_rescue import psd_convert

    return psd_convert.run(args)      # the converter finds the manifest itself


def cmd_sources(args: argparse.Namespace) -> int:
    LOG.info("%-14s %-48s %s", "AD", "AÇIKLAMA", "OTURUM")
    for cls in REGISTRY:
        LOG.info("%-14s %-48s %s", cls.name, cls.title, "gerekli" if cls.needs_auth else "-")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from adobe_rescue.gui import main as gui_main
    return gui_main()


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adobe-rescue",
        description="Adobe hesap geçişi öncesi toplu yedekleme aracı.")
    parser.add_argument("--version", action="version", version=f"adobe-cc-rescue {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="ayrıntılı günlük")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--output", help="yedek klasörü (varsayılan: Masaüstü/Adobe-Backup-TARIH)")
        p.add_argument("--config", help="JSON ayar dosyası")
        p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("doctor", help="ortamı kontrol et")
    add_common(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="Adobe'ye giriş yap (pencere açılır)")
    p.add_argument("--token", help="[gelişmiş] Bearer jetonu")
    p.add_argument("--har", help="[gelişmiş] DevTools HAR dosyası (uç noktaları da çıkarır)")
    p.add_argument("--clear", action="store_true", help="kayıtlı oturumu sil")
    p.add_argument("--browser-worker", action="store_true",
                   help=argparse.SUPPRESS)   # internal: child process showing the window
    p.add_argument("--out", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("pull", help="yedeği indir")
    add_common(p)
    p.add_argument("--only", action="append", help="yalnızca bu kaynaklar (virgülle)")
    p.add_argument("--skip", action="append", help="bu kaynakları atla (virgülle)")
    p.add_argument("--local-only", action="store_true", help="yalnızca yerel kaynaklar")
    p.add_argument("--include", action="append", default=[], help="dosya deseni (glob)")
    p.add_argument("--exclude", action="append", default=[], help="hariç tutulacak desen")
    p.add_argument("--max-size-mb", type=float, help="bu boyuttan büyük dosyaları atla")
    p.add_argument("--workers", type=int, help="eşzamanlı indirme sayısı (varsayılan 6)")
    p.add_argument("--timeout", type=float, help="istek zaman aşımı (sn)")
    p.add_argument("--retries", type=int, help="deneme sayısı")
    p.add_argument("--rate-limit", type=float, help="istekler arası bekleme (sn)")
    p.add_argument("--retry-failed", action="store_true", help="hatalıları tekrar dene")
    p.add_argument("--resume", action="store_true", default=True,
                   help="inen dosyaları atla (varsayılan)")
    p.add_argument("--no-resume", action="store_true",
                   help="her şeyi baştan indir (yavaş)")
    p.add_argument("--overwrite", action="store_true", help="mevcut dosyaların üzerine yaz")
    p.add_argument("--no-verify", action="store_true", help="sha256 doğrulamasını kapat")
    p.add_argument("--dry-run", action="store_true", help="indirme, sadece listele")
    p.add_argument("--open", action="store_true", help="bitince klasörü aç")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("verify", help="yedeği sağlama topla")
    add_common(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("package", help="yedeği tek ZIP yap")
    add_common(p)
    p.add_argument("--archive", help="hedef zip yolu")
    p.add_argument("--store", action="store_true", help="sıkıştırma yapma (daha hızlı)")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("report", help="raporu yeniden üret")
    add_common(p)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("discover",
                       help="HAR'daki Adobe API adreslerini listele (jeton gösterilmez)")
    p.add_argument("--har", help="DevTools HAR dosyası")
    p.add_argument("--captured", action="store_true",
                   help="giriş penceresinde kaydedilen istekleri göster (HAR gerekmez)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("psd",
                       help="bulut belgelerini katmanlı PSD'ye çevir (Windows + Photoshop)")
    p.add_argument("--out", required=True, help="PSD dosyalarının yazılacağı klasör")
    p.add_argument("--names", help="yedekteki manifest.json (verilmezse aranır)")
    p.add_argument("--watch", action="store_true",
                   help="belgeleri siz açın (Photoshop 28'den eskiyse)")
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--include-local", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cloud-root")
    p.add_argument("--limit", type=int, help="yalnızca ilk N belge")
    p.add_argument("--color-settings", default="",
                   help="geçici renk ayarı profili (sonunda geri alınır)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_psd)

    p = sub.add_parser("sources", help="kaynakları listele")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("gui", help="pencereli arayüzü aç")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    if not getattr(args, "command", None):
        # Double-clicked binary with no arguments → open the GUI.
        if not sys.stdin or not sys.stdin.isatty():
            try:
                return cmd_gui(args)
            except Exception:
                pass
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except AuthError as exc:
        LOG.error("Oturum hatası: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.warning("\nİptal edildi.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
