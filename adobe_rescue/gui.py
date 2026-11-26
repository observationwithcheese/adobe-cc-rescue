"""Tkinter GUI — the double-click experience for non-technical users.

Deliberately stdlib-only so the frozen .exe / .app stays small and needs no
runtime beyond what PyInstaller bundles.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from adobe_rescue import __version__, config
from adobe_rescue.auth import AuthError, Session, from_har, from_token
from adobe_rescue.config import Settings
from adobe_rescue.engine import Engine, Progress
from adobe_rescue.manifest import STATUS_MANUAL
from adobe_rescue.report import write_checklist, write_html
from adobe_rescue.sources import REGISTRY
from adobe_rescue.util import LOG, human_delta, human_size, open_in_file_manager

PAD = 10


class QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:  # pragma: no cover
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Adobe CC Rescue {__version__}")
        self.root.minsize(760, 620)

        self.session = Session.load()
        self.settings = Settings()
        self.engine: Optional[Engine] = None
        self.worker: Optional[threading.Thread] = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.source_vars: Dict[str, tk.BooleanVar] = {}

        self._build()
        self._attach_logging()
        self._refresh_session_label()
        self.root.after(120, self._drain_log)

    # --------------------------------------------------------------- build
    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=PAD)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(header, text="Adobe hesap geçişi yedekleyici",
                  font=("", 15, "bold")).pack(side="left")
        self.deadline_label = ttk.Label(header, text=self._deadline_text(), foreground="#b7791f")
        self.deadline_label.pack(side="right")

        # --- 1. session
        box = ttk.LabelFrame(main, text="1 · Adobe oturumu (bulut yedeği için)", padding=PAD)
        box.pack(fill="x", pady=(PAD, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.login_btn = ttk.Button(row, text="Adobe ile giriş yap",
                                    command=self._browser_login)
        self.login_btn.pack(side="left")
        self.logout_btn = ttk.Button(row, text="Çıkış", command=self._logout, width=8)
        self.logout_btn.pack(side="left", padx=(6, 0))
        self.session_label = ttk.Label(row, text="")
        self.session_label.pack(side="left", padx=(PAD, 0))

        ttk.Label(box, foreground="#6b6862", wraplength=640, justify="left",
                  text=("Adobe'nin kendi giriş sayfası açılır; şifreniz yalnızca Adobe'ye "
                        "gider, bu uygulama görmez. Giriş yapınca pencere kendiliğinden "
                        "kapanır.")).pack(fill="x", pady=(6, 0))

        # Advanced: manual token / HAR, hidden unless someone needs it.
        self.advanced_open = tk.BooleanVar(value=False)
        self.advanced_btn = ttk.Button(box, text="▸ Gelişmiş",
                                       command=self._toggle_advanced, width=14)
        self.advanced_btn.pack(anchor="w", pady=(8, 0))

        self.advanced = ttk.Frame(box)
        ttk.Label(self.advanced, text="Jeton:").grid(row=0, column=0, sticky="w")
        self.token_var = tk.StringVar()
        ttk.Entry(self.advanced, textvariable=self.token_var, show="•", width=46).grid(
            row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(self.advanced, text="Kaydet", command=self._save_token).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Button(self.advanced, text="HAR dosyasından al…", command=self._load_har).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Button(self.advanced, text="Nasıl alınır?", command=self._show_help).grid(
            row=1, column=2, padx=(6, 0), pady=(6, 0))
        self.advanced.columnconfigure(1, weight=1)

        # --- 2. sources
        box = ttk.LabelFrame(main, text="2 · Neler yedeklensin", padding=PAD)
        box.pack(fill="x", pady=(PAD, 0))
        for index, cls in enumerate(REGISTRY):
            var = tk.BooleanVar(value=True)
            self.source_vars[cls.name] = var
            label = cls.title + (" (oturum gerekli)" if cls.needs_auth else "")
            ttk.Checkbutton(box, text=label, variable=var).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, PAD))
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)

        # --- 3. destination
        box = ttk.LabelFrame(main, text="3 · Hedef klasör", padding=PAD)
        box.pack(fill="x", pady=(PAD, 0))
        self.output_var = tk.StringVar(value=str(self.settings.output))
        ttk.Entry(box, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(box, text="Seç…", command=self._pick_output).grid(row=0, column=1, padx=(6, 0))
        self.resume_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, variable=self.resume_var,
                        text="İnen dosyaları tekrar indirme (kesilirse kaldığı yerden devam et)"
                        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(box, text="Eşzamanlı indirme:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.workers_var = tk.IntVar(value=self.settings.workers)
        ttk.Spinbox(box, from_=1, to=16, textvariable=self.workers_var, width=5).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        box.columnconfigure(0, weight=1)

        # --- actions
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(PAD, 0))
        self.start_btn = ttk.Button(actions, text="Yedeklemeyi başlat", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Durdur", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Klasörü aç", command=self._open_output).pack(side="left", padx=(6, 0))
        self.report_btn = ttk.Button(actions, text="Raporu aç", command=self._open_report,
                                     state="disabled")
        self.report_btn.pack(side="left", padx=(6, 0))
        self.psd_btn = ttk.Button(actions, text="Bulut belgelerini PSD'ye çevir",
                                  command=self._convert_psd)
        self.psd_btn.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(PAD, 2))
        self.status_label = ttk.Label(main, text="Hazır.")
        self.status_label.pack(fill="x")

        logbox = ttk.LabelFrame(main, text="Günlük", padding=6)
        logbox.pack(fill="both", expand=True, pady=(PAD, 0))
        self.log_text = tk.Text(logbox, height=12, wrap="none", font=("Menlo", 10))
        scroll = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------ plumbing
    def _attach_logging(self) -> None:
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.setLevel(logging.INFO)
        LOG.addHandler(handler)

    def _drain_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _deadline_text(self) -> str:
        due = config.deadline()
        if not due:
            return ""
        left = due - datetime.now()
        if left.total_seconds() <= 0:
            return "Son tarih geçti"
        return f"Son tarihe {human_delta(left)} kaldı"

    def _refresh_session_label(self) -> None:
        if not self.session.token:
            text, color = "Giriş yapılmadı — yalnızca yerel dosyalar yedeklenir.", "#b7791f"
        elif self.session.is_expired():
            text, color = "Jetonun süresi dolmuş — yenileyin.", "#c0392b"
        else:
            who = self.session.email or self.session.user_id or "hesap"
            left = human_delta(datetime.fromtimestamp(self.session.expires) - datetime.now()) \
                if self.session.expires else "?"
            text, color = f"Oturum hazır: {who} · {left} geçerli", "#2f7d4f"
        self.session_label.configure(text=text, foreground=color)

    # ------------------------------------------------------------- actions
    def _toggle_advanced(self) -> None:
        if self.advanced_open.get():
            self.advanced.pack_forget()
            self.advanced_btn.configure(text="▸ Gelişmiş")
            self.advanced_open.set(False)
        else:
            self.advanced.pack(fill="x", pady=(6, 0))
            self.advanced_btn.configure(text="▾ Gelişmiş")
            self.advanced_open.set(True)

    def _browser_login(self) -> None:
        """Open Adobe's own sign-in page in a child process."""
        from adobe_rescue import browser_login

        if not browser_login.webview_available():
            messagebox.showerror(
                "Giriş penceresi açılamadı",
                "Bu bilgisayarda giriş penceresi bileşeni yok.\n\n"
                "Windows'ta: Microsoft Edge WebView2 Runtime kurun.\n"
                "Alternatif: 'Gelişmiş' bölümünden jeton ile giriş yapın.")
            self._toggle_advanced()
            return

        self.login_btn.configure(state="disabled", text="Giriş bekleniyor…")
        self.session_label.configure(text="Açılan pencerede oturum açın…",
                                     foreground="#6b6862")

        def work() -> None:
            try:
                session = browser_login.login_via_subprocess()
            except Exception as exc:
                self.root.after(0, self._login_failed, str(exc))
                return
            self.root.after(0, self._login_done, session)

        threading.Thread(target=work, daemon=True).start()

    def _login_done(self, session) -> None:
        self.session = session
        self.session.save()
        self.login_btn.configure(state="normal", text="Adobe ile giriş yap")
        self._refresh_session_label()
        self._check_access()

    def _check_access(self) -> None:
        """Confirm Adobe actually accepts the session before a backup starts."""
        from adobe_rescue.selftest import check_cloud_access

        self.session_label.configure(text="Bulut erişimi kontrol ediliyor…",
                                     foreground="#6b6862")

        def work() -> None:
            ok, message = check_cloud_access(self.session)
            self.root.after(0, self._access_result, ok, message)

        threading.Thread(target=work, daemon=True).start()

    def _access_result(self, ok: bool, message: str) -> None:
        self._refresh_session_label()
        if ok:
            LOG.info("Bağlantı testi başarılı: %s", message)
            return
        LOG.warning("Bağlantı testi başarısız: %s", message)
        messagebox.showwarning(
            "Bulut erişimi doğrulanamadı",
            message + "\n\nYerel kaynaklar (Creative Cloud Files klasörü, ön "
                      "ayarlar, fontlar) yine de yedeklenebilir.")

    def _login_failed(self, message: str) -> None:
        self.login_btn.configure(state="normal", text="Adobe ile giriş yap")
        self._refresh_session_label()
        messagebox.showerror("Giriş tamamlanmadı", message)

    def _logout(self) -> None:
        self.session.clear()
        self.session = Session()
        self._refresh_session_label()

    def _save_token(self) -> None:
        raw = self.token_var.get().strip()
        if not raw:
            return
        try:
            self.session = from_token(raw)
            self.session.save()
        except AuthError as exc:
            messagebox.showerror("Jeton hatası", str(exc))
            return
        self.token_var.set("")
        self._refresh_session_label()

    def _load_har(self) -> None:
        path = filedialog.askopenfilename(title="HAR dosyası seçin",
                                          filetypes=[("HAR", "*.har"), ("Tümü", "*.*")])
        if not path:
            return
        try:
            self.session = from_har(Path(path))
            self.session.save()
        except AuthError as exc:
            messagebox.showerror("HAR hatası", str(exc))
            return
        self._refresh_session_label()
        messagebox.showinfo("Tamam", "Oturum ve uç noktalar HAR dosyasından alındı.")

    def _show_help(self) -> None:
        messagebox.showinfo("Jeton nasıl alınır", (
            "1. Chrome ile https://assets.adobe.com adresine girip oturum açın.\n"
            "2. F12 > Network sekmesi > sayfayı yenileyin.\n"
            "3. adobe.io ile biten bir isteğe tıklayın.\n"
            "4. Headers > Request Headers > 'authorization: Bearer eyJ...' satırına\n"
            "   SAĞ TIKLAYIN > 'Copy value' > buraya yapıştırın.\n"
            "   (Fareyle seçip kopyalamayın: Chrome uzun başlıkları '...' ile kısaltır\n"
            "    ve jeton eksik gelir.)\n\n"
            "Alternatif: Network sekmesinde sağ tık > 'Save all as HAR with content'\n"
            "ve 'HAR dosyasından al…' düğmesini kullanın."))

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="Yedek klasörü")
        if path:
            self.output_var.set(path)

    def _open_output(self) -> None:
        open_in_file_manager(Path(self.output_var.get()))

    def _convert_psd(self) -> None:
        """Run the Photoshop conversion over the documents in this backup."""
        import sys as _sys

        if not _sys.platform.startswith("win"):
            messagebox.showinfo("Yalnızca Windows",
                                "PSD'ye çevirme Windows'ta, Photoshop açıkken çalışır.")
            return

        from adobe_rescue.psd_convert import find_manifest

        backup = Path(self.output_var.get()).expanduser()
        manifest = find_manifest(backup)
        if manifest is None:
            messagebox.showwarning(
                "Önce yedek alın",
                f"{backup} klasöründe (ve Masaüstündeki Adobe-Backup-* "
                "klasörlerinde) bulut belgesi listesi bulunamadı.\n\n"
                "Önce yedeklemeyi çalıştırın; çevirme işlemi oradaki listeyi kullanır.")
            return
        backup = manifest.parent
        if not messagebox.askokcancel(
                "Bulut belgelerini PSD'ye çevir",
                "Photoshop (28+) açık ve iş hesabınızla oturum açmış olmalı.\n\n"
                "Her belge tek tek açılıp katmanlı .psd olarak kaydedilecek. "
                "Bulut belgeleriniz değiştirilmez.\n\n"
                "Bu sırada Photoshop'u kullanmayın. Başlansın mı?"):
            return

        out_dir = backup / "psd"
        self.psd_btn.configure(state="disabled", text="Çevriliyor…")
        self.status_label.configure(text="Bulut belgeleri PSD'ye çevriliyor…")

        def work() -> None:
            from adobe_rescue import psd_convert

            argv = ["--out", str(out_dir), "--names", str(manifest)]
            try:
                code = psd_convert.main(argv)
            except Exception as exc:
                self.root.after(0, self._psd_done, None, str(exc))
                return
            self.root.after(0, self._psd_done, code, "")

        threading.Thread(target=work, daemon=True).start()

    def _psd_done(self, code, error: str) -> None:
        self.psd_btn.configure(state="normal", text="Bulut belgelerini PSD'ye çevir")
        out_dir = Path(self.output_var.get()).expanduser() / "psd"
        if error:
            self.status_label.configure(text="Çevirme başarısız.")
            messagebox.showerror("Çevirme başarısız", error)
            return
        self.status_label.configure(text=f"PSD çevirme bitti → {out_dir}")
        messagebox.showinfo("Bitti", f"PSD dosyaları:\n{out_dir}\n\n"
                                     "Rapor güncellendi. Ayrıntılar için günlüğe bakın.")
        self.report_btn.configure(state="normal")
        open_in_file_manager(out_dir)

    def _open_report(self) -> None:
        report = Path(self.output_var.get()) / "rapor.html"
        if report.exists():
            webbrowser.open(report.as_uri())

    def _selected_sources(self) -> List[str]:
        names = [name for name, var in self.source_vars.items() if var.get()]
        if not self.session.token:
            names = [n for n in names if not _needs_auth(n)]
        return names

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        names = self._selected_sources()
        if not names:
            messagebox.showwarning("Kaynak yok", "En az bir kaynak seçin.")
            return

        self.settings.output = Path(self.output_var.get()).expanduser()
        self.settings.workers = max(1, int(self.workers_var.get()))
        self.settings.resume = bool(self.resume_var.get())
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=100)
        self.status_label.configure(text="Başlatılıyor…")

        try:
            from adobe_rescue.util import setup_logging
            self.settings.output.mkdir(parents=True, exist_ok=True)
            setup_logging(False, self.settings.output / "adobe-rescue.log")
            self._attach_logging()      # keep the on-screen log working too
        except OSError:
            pass

        self.engine = Engine(self.settings,
                             self.session if self.session.token else None,
                             on_progress=self._on_progress)
        self.worker = threading.Thread(target=self._run, args=(names,), daemon=True)
        self.worker.start()

    def _run(self, names: List[str]) -> None:
        engine = self.engine
        assert engine is not None
        try:
            result = engine.run(names)
        except AuthError as exc:
            # `exc` is unbound once the except block exits, so capture it now.
            message = str(exc)
            self.root.after(0, lambda m=message: messagebox.showerror("Oturum hatası", m))
            self.root.after(0, self._finish, None)
            return
        except Exception as exc:  # pragma: no cover - surfaced to the user
            message = str(exc)
            self.root.after(0, lambda m=message: messagebox.showerror("Hata", m))
            self.root.after(0, self._finish, None)
            return
        finally:
            engine.close()

        engine.manifest.export_csv()
        write_html(engine.manifest, result, self.settings.output / "rapor.html")
        write_checklist(engine.manifest, self.settings.output / "YAPILACAKLAR.md")
        self.root.after(0, self._finish, result)

    def _on_progress(self, progress: Progress) -> None:
        def update() -> None:
            # Drive the bar with BYTES where we know them: with one 145 MB
            # file left, a file-count bar sits still and looks frozen.
            if progress.bytes_expected:
                self.progress.configure(maximum=progress.bytes_expected,
                                        value=min(progress.bytes_seen,
                                                  progress.bytes_expected))
            elif progress.total:
                self.progress.configure(maximum=progress.total, value=progress.done)

            parts = [f"{progress.source}", f"{progress.done}/{progress.total} dosya"]
            if progress.bytes_expected:
                parts.append(f"{human_size(progress.bytes_seen)} / "
                             f"{human_size(progress.bytes_expected)}")
            if progress.speed > 1024:
                parts.append(f"{human_size(progress.speed)}/sn")
                eta = progress.eta_seconds()
                if eta > 60:
                    parts.append(f"~{int(eta // 60)} dk kaldı")
                elif eta > 1:
                    parts.append(f"~{int(eta)} sn kaldı")
            if progress.item:
                parts.append(progress.item)
            self.status_label.configure(text=" · ".join(parts))
        self.root.after(0, update)

    def _stop(self) -> None:
        if self.engine:
            self.engine.stop()
        self.status_label.configure(text="Durduruluyor…")

    def _finish(self, result) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.report_btn.configure(state="normal")
        if result is None:
            self.status_label.configure(text="Yarıda kesildi.")
            return
        manual = len(self.engine.manifest.by_status(STATUS_MANUAL)) if self.engine else 0
        self.status_label.configure(
            text=f"Bitti · {result.downloaded} dosya · {human_size(result.bytes_total)} · "
                 f"{result.failed} hata · {manual} elle yapılacak")
        messagebox.showinfo("Yedek tamamlandı", (
            f"{result.downloaded} dosya indirildi ({human_size(result.bytes_total)}).\n"
            f"{result.failed} hata, {manual} öğe elle yapılmalı.\n\n"
            f"Ayrıntılar: rapor.html ve YAPILACAKLAR.md"))
        self._open_report()


def _needs_auth(name: str) -> bool:
    for cls in REGISTRY:
        if cls.name == name:
            return cls.needs_auth
    return False


def main() -> int:
    # The frozen binary re-invokes itself with arguments (see app.py); if this
    # module is the entry point, honour that instead of opening a second window.
    if sys.argv[1:]:
        from adobe_rescue.cli import main as cli_main
        return cli_main(sys.argv[1:])
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
