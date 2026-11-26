"""Turn open Photoshop cloud documents into layered local PSD files.

Photoshop will not let a script OPEN a cloud document (every documented and
undocumented route refuses), but it will happily save one. So the work
splits: you open documents — File > Open Cloud Document takes a
multi-selection — and this converts and closes each one, layers intact.

    python tools/ps_batch_psd.py --out "C:\\Users\\you\\Desktop\\PSD" --watch

SAFETY, because this closes documents:
  * Only documents it positively identifies as cloud documents are touched.
    A local file you have open is left alone.
  * A document with unsaved changes is NEVER closed, cloud or not.
  * Documents are saved AS A COPY and closed WITHOUT saving, so the cloud
    original is unchanged.
  * --names restricts it to a known list (e.g. the 61 from your backup), so
    nothing else can be caught by accident. Recommended.
  * --dry-run shows exactly what it would do and touches nothing.

Windows only; needs `pip install pywin32`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PROGIDS = [
    "Photoshop.Application",
    *(f"Photoshop.Application.{v}" for v in
      (200, 190, 185, 180, 175, 170, 160, 150, 140, 130, 120)),
]

UNTITLED = re.compile(r"^(untitled|adsız|isimsiz)[-_ ]?\d*$", re.I)
#: Photoshop shows a duplicate as "Balcony_Bg (1)"; the manifest has the
#: original name, so matching must ignore that suffix.
COPY_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
#: Characters Windows will not accept in a file name.
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: ExtendScript has no JSON.stringify, so the array is built by hand.
LIST_JS = r"""
(function () {
  var out = [];
  for (var i = 0; i < app.documents.length; i++) {
    var d = app.documents[i];
    var p = "", hasPath = true, cloud = false, saved = true;
    try { p = d.fullName.fsName; } catch (e) { hasPath = false; }
    try { cloud = (d.cloudDocument === true); } catch (e) { cloud = false; }
    try { saved = (d.saved === true); } catch (e) { saved = false; }
    out.push('{"name":"' + String(d.name).replace(/\\/g, "\\\\").replace(/"/g, '\\"')
      + '","saved":' + (saved ? "true" : "false")
      + ',"hasPath":' + (hasPath ? "true" : "false")
      + ',"cloud":' + (cloud ? "true" : "false")
      + ',"path":"' + String(p).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"}');
  }
  return "[" + out.join(",") + "]";
})();
"""

AUTO_JS = r"""
(function () {
  // Photoshop stops on modal dialogs — embedded profile mismatch, missing
  // profile, missing fonts, text-layer updates — and an unattended run then
  // waits for a human forever. Suppressing dialogs makes Photoshop apply its
  // colour-settings POLICY instead of asking, which preserves the embedded
  // profile under every stock preset. The previous value is always restored.
  var prevDialogs = app.displayDialogs;
  var prevColor = null;
  var result = "";

  try {
    app.displayDialogs = DialogModes.NO;
    if (COLORSETTINGS !== "") {
      try { prevColor = app.colorSettings; app.colorSettings = COLORSETTINGS; }
      catch (e) { prevColor = null; }
    }

    var existing = null;
    for (var i = 0; i < app.documents.length; i++) {
      if (app.documents[i].name === DOCNAME) { existing = app.documents[i]; break; }
    }

    var d = existing, opened = false;
    if (d === null) {
      try {
        d = app.open(new File(CLOUDPATH));   // forward slashes only
        opened = true;
      } catch (e) {
        return "OPENFAIL|" + DOCNAME + "|" + String(e);
      }
    }

    if (d.cloudDocument !== true) {
      if (opened) { d.close(SaveOptions.DONOTSAVECHANGES); }
      return "NOTCLOUD|" + DOCNAME;
    }
    if (d.saved !== true) {
      return "DIRTY|" + DOCNAME;             // never close someone's edits
    }

    var layers = 0, profile = "";
    try { layers = d.layers.length; } catch (e) { layers = 0; }
    try { profile = String(d.colorProfileName); } catch (e) { profile = ""; }

    var psdError = "";
    try {
      var o = new PhotoshopSaveOptions();
      o.layers = true; o.alphaChannels = true; o.annotations = true;
      o.embedColorProfile = true; o.spotColors = true;
      var out = new File(OUTPATH);
      d.saveAs(out, o, true, Extension.LOWERCASE);
      if (opened) { d.close(SaveOptions.DONOTSAVECHANGES); }
      return "SAVED|" + out.fsName + "|" + out.length + "|" + layers + "|" + profile
             + (opened ? "" : "|KEPTOPEN");
    } catch (e) {
      psdError = String(e);
    }
    try {
      var big = new File(OUTPATH.replace(/\.psd$/i, ".psb"));
      var bo = new LargeDocumentSaveOptions();
      bo.layers = true; bo.alphaChannels = true; bo.embedColorProfile = true;
      d.saveAs(big, bo, true, Extension.LOWERCASE);
      if (opened) { d.close(SaveOptions.DONOTSAVECHANGES); }
      return "SAVED|" + big.fsName + "|" + big.length + "|" + layers + "|" + profile
             + (opened ? "" : "|KEPTOPEN");
    } catch (e2) {
      // Leave it open: never close a document we could not preserve.
      return "FAIL|" + DOCNAME + "|psd: " + psdError + " | psb: " + String(e2);
    }
  } finally {
    app.displayDialogs = prevDialogs;
    if (prevColor !== null) {
      try { app.colorSettings = prevColor; } catch (e) {}
    }
  }
})();
"""

SAVE_JS = r"""
(function () {
  var prevDialogs = app.displayDialogs;
  try {
  app.displayDialogs = DialogModes.NO;
  var target = null;
  for (var i = 0; i < app.documents.length; i++) {
    if (app.documents[i].name === DOCNAME) { target = app.documents[i]; break; }
  }
  if (target === null) { return "GONE|" + DOCNAME; }
  if (target.saved !== true) { return "DIRTY|" + DOCNAME; }

  var out = new File(OUTPATH);
  var psdError = "";
  try {
    var o = new PhotoshopSaveOptions();
    o.layers = true; o.alphaChannels = true; o.annotations = true;
    o.embedColorProfile = true; o.spotColors = true;
    target.saveAs(out, o, true, Extension.LOWERCASE);
    target.close(SaveOptions.DONOTSAVECHANGES);
    return "SAVED|" + out.fsName + "|" + out.length;
  } catch (e) {
    psdError = String(e);
  }
  try {
    // Over 2 GB only PSB can hold the document.
    var big = new File(OUTPATH.replace(/\.psd$/i, ".psb"));
    var bo = new LargeDocumentSaveOptions();
    bo.layers = true; bo.alphaChannels = true; bo.embedColorProfile = true;
    target.saveAs(big, bo, true, Extension.LOWERCASE);
    target.close(SaveOptions.DONOTSAVECHANGES);
    return "SAVED|" + big.fsName + "|" + big.length;
  } catch (e2) {
    // Leave the document OPEN: closing one we could not save risks the work.
    return "FAIL|" + DOCNAME + "|psd: " + psdError + " | psb: " + String(e2);
  }
})();
"""


#: COM refuses to attach an elevated client to a normal-integrity Photoshop.
SERVER_EXEC_FAILURE = -2146959355


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())   # type: ignore[attr-defined]
    except Exception:
        return False


def connect_error(errors: List[Tuple[str, int, str]], admin: bool) -> str:
    """Explain a failed connection without eleven lines of noise."""
    exec_failed = any(code == SERVER_EXEC_FAILURE for _, code, _ in errors)
    if exec_failed and admin:
        return ("Photoshop'a bağlanılamadı: bu komut YÖNETİCİ olarak çalışıyor, "
                "Photoshop ise normal kullanıcı olarak açık. Windows bu ikisini "
                "birbirine bağlamıyor.\n"
                "Çözüm: komutu yönetici olmayan normal bir komut penceresinden "
                "çalıştırın (ya da .exe'ye çift tıklayın).")
    if exec_failed:
        return ("Photoshop'a bağlanılamadı: COM sunucusu başlatılamadı. "
                "Photoshop'u açıp bir belge yükleyin, sonra tekrar deneyin.")
    first = errors[0][2] if errors else ""
    return ("Photoshop'a bağlanılamadı. Photoshop açık mı ve iş hesabınızla "
            "oturum açmış mı?" + (f"\n  {first}" if first else ""))


def connect() -> Tuple[object, str]:
    import win32com.client

    errors: List[Tuple[str, int, str]] = []
    for progid in PROGIDS:
        try:
            return win32com.client.Dispatch(progid), progid
        except Exception as exc:
            code = getattr(exc, "hresult", None)
            if code is None:
                args = getattr(exc, "args", ())
                code = args[0] if args and isinstance(args[0], int) else 0
            errors.append((progid, int(code or 0), str(exc)[:90]))
    raise SystemExit(connect_error(errors, is_admin()))


#: Photoshop rejects COM calls while it is busy downloading a cloud document.
BUSY_HRESULTS = (-2147417846, -2147418111)      # RETRYLATER, CALL_REJECTED


def run_js(ps, code: str, timeout: float = 240.0) -> str:
    """DoJavaScript via raw IDispatch, waiting out a busy Photoshop.

    Dynamic dispatch mis-detects DoJavaScript as a property and makepy
    refuses this type library, hence _oleobj_. Opening a cloud document
    blocks for tens of seconds and every call during that window fails with
    RPC_E_SERVERCALL_RETRYLATER, so those are waited out rather than raised.
    """
    import pythoncom

    deadline = time.monotonic() + timeout
    delay = 1.0
    while True:
        try:
            oleobj = ps._oleobj_
            dispid = oleobj.GetIDsOfNames("DoJavaScript")
            result = oleobj.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, code)
            return str(result) if result is not None else ""
        except Exception as exc:
            hresult = getattr(exc, "hresult", None) or getattr(exc, "args", [None])[0]
            if hresult not in BUSY_HRESULTS or time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)


def js_string(value: str) -> str:
    return json.dumps(str(value))


def list_documents(ps) -> List[Dict[str, object]]:
    raw = run_js(ps, LIST_JS)
    try:
        return json.loads(raw) if raw.strip().startswith("[") else []
    except ValueError:
        return []


def base_name(name: str) -> str:
    return re.sub(r"\.(psdc|psd|psb|aic|ai)$", "", str(name), flags=re.I)


def match_key(name: str) -> str:
    """Comparison key: no extension, no "(1)" copy suffix, case-folded.

    Document names here are Turkish and full of punctuation ("Firefly
    detaylı oda, fazla eşya"), so this must not strip anything but the
    things that genuinely differ between the dialog and the manifest.
    """
    return COPY_SUFFIX.sub("", base_name(name)).strip().lower()


def safe_stem(name: str, max_len: int = 120) -> str:
    """A file name Windows will accept, keeping Turkish characters."""
    stem = ILLEGAL.sub("_", base_name(name)).strip().strip(" .\u00a0")
    if not stem:
        stem = "belge"
    return stem[:max_len].strip() or "belge"


def eligibility(doc: Dict[str, object], allowed: Optional[Set[str]],
                include_local: bool) -> Tuple[bool, str]:
    """May we convert and close this document?

    `cloudDocument` is the authoritative signal — confirmed present on
    Photoshop 28. Note that a cloud document DOES return a fullName (a
    synthetic "C:\\cloud-content\\repo:~\\..." path), so a path is not
    evidence of a local file, and a brand-new unsaved document has no path
    at all while reporting saved === true. Requiring cloudDocument is the
    only rule that separates all three safely.
    """
    name = str(doc.get("name", ""))
    stem = base_name(name)

    if not doc.get("saved", False):
        return False, "kaydedilmemiş değişiklik var"

    if not doc.get("cloud"):
        if not include_local:
            return False, "bulut belgesi değil"
        if not doc.get("hasPath") or UNTITLED.match(stem):
            return False, "yeni/adsız belge"
        if allowed is not None and match_key(name) not in allowed:
            return False, "listede değil"
        return True, "yerel (izin verildi)"

    if allowed is not None:
        if match_key(name) in allowed:
            return True, "listede"
        return False, "listede değil"
    return True, "bulut belgesi"


def load_names(path: Path, sources: Set[str] = frozenset({"cloud-docs"})) -> Set[str]:
    """Cloud-document names from a backup manifest.

    Only rows from the cloud-docs source: a manifest also lists presets,
    fonts and json blobs, and widening the allowlist to all of them defeats
    the point of having one.
    """
    names: Set[str] = set()
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        entries = data.get("entries", data if isinstance(data, list) else [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if sources and str(entry.get("source", "")) not in sources:
                continue
            if entry.get("name"):
                names.add(match_key(str(entry["name"])))
    else:
        for line in text.splitlines()[1:]:
            parts = [p.strip() for p in line.split(";")]
            if len(parts) > 1 and (not sources or parts[0] in sources):
                names.add(match_key(parts[1]))
    return {n for n in names if n}


#: Photoshop reports cloud documents under a synthetic root.
DEFAULT_CLOUD_ROOT = "C:/cloud-content/repo:~"


def cloud_documents(path: Path,
                    sources: Set[str] = frozenset({"cloud-docs"})) -> List[Dict[str, str]]:
    """Documents to convert, as {name, repo_path}, from a backup manifest.

    The manifest's relative path encodes the cloud folder structure, so the
    repository path can be rebuilt without a live session: a row stored at
    "cloud-docs/cloud-content/Doc.psdc" came from "/cloud-content/Doc.psdc".
    """
    out: List[Dict[str, str]] = []
    seen: Set[str] = set()
    data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    entries = data.get("entries", data if isinstance(data, list) else [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if sources and str(entry.get("source", "")) not in sources:
            continue
        name = str(entry.get("name") or "")
        if not name.lower().endswith((".psdc", ".aic")):
            continue                      # previews and manual rows are not documents
        rel = str(entry.get("path") or "").replace("\\", "/")
        parts = [p for p in rel.split("/") if p]
        folders = parts[1:-1] if len(parts) > 1 else []      # drop "cloud-docs" and the file
        repo_path = "/" + "/".join(folders + [name]) if folders else "/" + name
        if repo_path in seen:
            continue
        seen.add(repo_path)
        out.append({"name": name, "repo_path": repo_path})
    return out


def find_manifest(out_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate a backup manifest that actually contains cloud documents.

    The default backup folder is date-stamped, so "today's" folder is the
    wrong guess the moment a run crosses midnight — which is exactly when
    this gets used. Search beside the output first, then every
    Adobe-Backup-* folder, newest first.
    """
    candidates: List[Path] = []
    if out_dir:
        candidates += [out_dir / "manifest.json", out_dir.parent / "manifest.json"]

    roots = []
    try:
        home = Path.home()
        roots = [home / "Desktop", home / "Masaüstü", home]
    except Exception:
        pass
    for root in roots:
        try:
            if root.is_dir():
                candidates += sorted(root.glob("Adobe-Backup-*/manifest.json"),
                                     key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue

    for candidate in candidates:
        try:
            if candidate.is_file() and cloud_documents(candidate):
                return candidate
        except (OSError, ValueError):
            continue
    return None


def cloud_path_for(repo_path: str, root: str = DEFAULT_CLOUD_ROOT) -> str:
    """Full path Photoshop accepts. Forward slashes only: a backslash before
    'repo' becomes a carriage return inside the JS string literal."""
    return (root.rstrip("/") + "/" + repo_path.lstrip("/")).replace("\\", "/")


def detect_cloud_root(ps) -> str:
    """Learn the synthetic root from any cloud document already open."""
    try:
        raw = run_js(ps, LIST_JS, timeout=30)
        for doc in json.loads(raw):
            if doc.get("cloud") and doc.get("path"):
                folder = str(doc["path"]).replace("\\", "/")
                marker = "/repo:~"
                if marker in folder:
                    return folder[:folder.index(marker) + len(marker)]
    except Exception:
        pass
    return DEFAULT_CLOUD_ROOT


def photoshop_version(ps) -> Tuple[int, str]:
    try:
        raw = run_js(ps, "app.version;", timeout=30).strip()
        return int(raw.split(".")[0]), raw
    except Exception:
        return 0, "?"


def count_documents(path: Path,
                    sources: Set[str] = frozenset({"cloud-docs"})) -> int:
    """How many documents the manifest lists, before names are de-duplicated."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    total = 0
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        entries = data.get("entries", data if isinstance(data, list) else [])
        for entry in entries:
            if isinstance(entry, dict) and (not sources
                                            or str(entry.get("source", "")) in sources):
                total += 1
    else:
        for line in text.splitlines()[1:]:
            parts = [p.strip() for p in line.split(";")]
            if len(parts) > 1 and (not sources or parts[0] in sources):
                total += 1
    return total


def unique_target(out_dir: Path, stem: str, done: Set[str]) -> Optional[Path]:
    """Where this document should be written, or None if already converted."""
    target = out_dir / f"{stem}.psd"
    if not target.exists() and not (out_dir / f"{stem}.psb").exists():
        return target
    if stem.lower() in done:
        return None                      # we wrote it in this run: real resume
    for i in range(2, 100):              # same name, different document
        candidate = out_dir / f"{stem} ({i}).psd"
        if not candidate.exists():
            return candidate
    return None


def convert_auto(ps, docs: List[Dict[str, str]], out_dir: Path, root: str,
                 dry_run: bool, color_settings: str = "") -> Tuple[int, int, int]:
    """Open each cloud document by path, save a PSD, close it."""
    saved = skipped = failed = 0
    total = len(docs)

    for index, doc in enumerate(docs, 1):
        name = doc["name"]
        stem = safe_stem(name)
        target = out_dir / f"{stem}.psd"
        if target.exists() or (out_dir / f"{stem}.psb").exists():
            print(f"[{index}/{total}] — {stem} (zaten var, atlandı)")
            skipped += 1
            continue

        full = cloud_path_for(doc["repo_path"], root)
        if dry_run:
            print(f"[{index}/{total}] [deneme] {full}  ->  {target.name}")
            continue

        print(f"[{index}/{total}] {name} … ", end="", flush=True)
        started = time.monotonic()
        code = (f"var DOCNAME = {js_string(name)};\n"
                f"var CLOUDPATH = {js_string(full)};\n"
                f"var OUTPATH = {js_string(str(target))};\n"
                f"var COLORSETTINGS = {js_string(color_settings or '')};\n" + AUTO_JS)
        try:
            result = run_js(ps, code)
        except Exception as exc:
            failed += 1
            print(f"HATA ({str(exc)[:90]})")
            continue

        elapsed = time.monotonic() - started
        kind, _, rest = result.partition("|")
        fields = rest.split("|")
        if kind == "SAVED":
            saved += 1
            size = fields[1] if len(fields) > 1 else "0"
            layers = fields[2] if len(fields) > 2 else "?"
            profile = fields[3] if len(fields) > 3 and fields[3] != "KEPTOPEN" else ""
            kept = " (belge açık bırakıldı)" if "KEPTOPEN" in fields else ""
            detail = f"✓ {int(size):,} bayt · {layers} katman" if size.isdigit() else "✓"
            if profile:
                detail += f" · {profile}"
            print(f"{detail} · {elapsed:.0f} sn{kept}")
        elif kind == "DIRTY":
            skipped += 1
            print("atlandı (kaydedilmemiş değişiklik var)")
        elif kind == "NOTCLOUD":
            skipped += 1
            print("atlandı (bulut belgesi değil)")
        else:
            failed += 1
            print(f"HATA: {rest[:160]}")

    return saved, skipped, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--names", help="yedek klasöründeki manifest.json")
    parser.add_argument("--watch", action="store_true",
                        help="belgeleri siz açın, araç kaydedip kapatsın "
                             "(otomatik mod çalışmazsa)")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--include-local", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cloud-root", help=f"varsayılan: {DEFAULT_CLOUD_ROOT}")
    parser.add_argument("--limit", type=int, help="yalnızca ilk N belge (deneme için)")
    parser.add_argument("--color-settings", default="",
                        help="dönüştürme sırasında geçici renk ayarı profili "
                             "(örn. \"North America General Purpose 2\"); "
                             "işlem sonunda eski ayarınıza dönülür")
    return parser


def update_backup_report(manifest_path: Path, out_dir: Path,
                         converted: int, total: int) -> None:
    """Record the conversion in the backup and rewrite the report.

    Without this the report still tells the reader to convert 61 documents
    by hand, hours after they were converted — the one way a correct backup
    can still cost somebody an evening.
    """
    try:
        from adobe_rescue.engine import Result
        from adobe_rescue.manifest import Manifest, STATUS_DONE
        from adobe_rescue.report import write_checklist, write_html

        backup = manifest_path.parent
        manifest = Manifest(backup)
        entry = manifest.get("cloud-docs:__to-psd__")
        note = (f"{converted}/{total} bulut belgesi katmanlı PSD olarak kaydedildi: "
                f"{out_dir}")
        if entry is not None:
            entry.mark(STATUS_DONE, note)
            manifest.update(entry)
        manifest.save(force=True)
        manifest.export_csv()
        write_html(manifest, Result(), backup / "rapor.html")
        write_checklist(manifest, backup / "YAPILACAKLAR.md")
        print(f"Rapor güncellendi: {backup / 'rapor.html'}")
    except Exception as exc:                     # never fail a good run on this
        print(f"(Rapor güncellenemedi: {exc})")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


def run(args) -> int:
    if not sys.platform.startswith("win"):
        print("Bu araç yalnızca Windows'ta çalışır.")
        return 2

    out_dir = Path(args.out).expanduser()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    ps, progid = connect()
    major, version = photoshop_version(ps)
    print(f"Photoshop {version} bağlandı ({progid}) · hedef: {out_dir}")

    auto = not args.watch
    if auto and not args.names:
        found = find_manifest(out_dir)
        if found is None:
            print("Bulut belge listesi bulunamadı. Önce yedeklemeyi çalıştırın, "
                  "ya da --names ile manifest.json yolunu verin.\n"
                  "Belgeleri kendiniz açacaksanız --watch kullanın.")
            return 2
        args.names = str(found)
        print(f"Belge listesi: {found}")
    if auto and major and major < 28:
        print(f"Photoshop {version} betikle bulut belgesi açamıyor "
              f"(28 ve üstü gerekiyor). İzleme moduna geçiliyor: "
              f"belgeleri Dosya > Bulut Belgesi Aç ile siz açın.")
        auto = False

    if auto:
        docs = cloud_documents(Path(args.names).expanduser())
        if args.limit:
            docs = docs[: args.limit]
        if not docs:
            print("Manifestte bulut belgesi yok.")
            return 1
        root = args.cloud_root or detect_cloud_root(ps)
        print(f"{len(docs)} bulut belgesi · kök: {root}")
        print("Photoshop'a dokunmayın; belgeler tek tek açılıp kaydedilecek.\n")
        saved, skipped, failed = convert_auto(ps, docs, out_dir, root, args.dry_run,
                                              getattr(args, "color_settings", "") or "")
        print(f"\nBitti: {saved} kaydedildi · {skipped} atlandı · {failed} hata")
        print(f"Klasör: {out_dir}")
        if not args.dry_run and not failed:
            update_backup_report(Path(args.names).expanduser(), out_dir,
                                 saved + skipped, len(docs))
        return 0 if not failed else 1

    return run_watch(ps, args, out_dir)


def run_watch(ps, args, out_dir: Path) -> int:
    """Original mode: the user opens documents, this saves and closes them."""
    allowed = load_names(Path(args.names).expanduser()) if args.names else None
    if allowed is not None:
        total = count_documents(Path(args.names).expanduser())
        if total and total != len(allowed):
            print(f"Liste yüklendi: {total} belge ({len(allowed)} benzersiz ad; "
                  f'"(1)" kopyaları aynı ada denk geliyor, ayrı dosya olarak kaydedilir)')
        else:
            print(f"Liste yüklendi: {len(allowed)} belge")

    print("Photoshop'ta Dosya > Bulut Belgesi Aç ile belgeleri açın.")
    if args.dry_run:
        print("DENEME MODU: hiçbir belge kaydedilmez veya kapatılmaz.\n")
    else:
        print("Siz açtıkça kaydedip kapatıyorum. Bitince Ctrl+C.\n")

    saved = skipped = failed = 0
    done: Set[str] = set()
    reported: Set[str] = set()

    try:
        while True:
            docs = list_documents(ps)
            progressed = False

            for doc in docs:
                name = str(doc.get("name", ""))
                stem = safe_stem(name)
                ok, why = eligibility(doc, allowed, args.include_local)
                if not ok:
                    if name not in reported:
                        print(f"  — {name}: dokunulmadı ({why})")
                        reported.add(name)
                    continue

                target = unique_target(out_dir, stem, done)
                if target is None:
                    if name not in reported:
                        print(f"  — {stem} (zaten dönüştürülmüş, atlandı)")
                        reported.add(name)
                    skipped += 1
                    continue

                if args.dry_run:
                    print(f"  [deneme] {name} -> {target.name}  ({why})")
                    reported.add(name)
                    continue

                code = (f"var DOCNAME = {js_string(name)};\n"
                        f"var OUTPATH = {js_string(str(target))};\n" + SAVE_JS)
                result = run_js(ps, code)
                kind, _, rest = result.partition("|")
                if kind == "SAVED":
                    size = rest.split("|")[-1]
                    saved += 1
                    done.add(stem.lower())
                    progressed = True
                    print(f"  ✓ {target.name}"
                          + (f"  ({int(size):,} bayt)" if size.isdigit() else ""))
                elif kind in ("DIRTY", "GONE"):
                    print(f"  — {name}: "
                          + ("kaydedilmemiş değişiklik" if kind == "DIRTY" else "kapanmış"))
                    reported.add(name)
                else:
                    failed += 1
                    progressed = True
                    print(f"  ! {rest}")
                break

            if args.dry_run or not progressed:
                if args.dry_run:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nDurduruldu.")

    print(f"\nBitti: {saved} kaydedildi · {skipped} atlandı · {failed} hata")
    print(f"Klasör: {out_dir}")
    return 0 if not failed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
