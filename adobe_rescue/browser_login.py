"""Embedded Adobe sign-in.

The user clicks one button, a real Adobe login page opens in an OS webview
(WebView2 on Windows, WKWebView on macOS), they sign in exactly as they would
in a browser — SSO, 2FA, company IdP, all of it — and the window closes by
itself. No DevTools, no copy-paste, no token ever shown.

How it works: once the Creative Cloud web app finishes loading, Adobe's own
IMS library has already stored the session token in the page's localStorage.
We read it from there, and nothing else: no keystrokes, no password, no
network interception.

The webview must own the main thread, which tkinter already owns in the GUI,
so the window runs in a short-lived child process that writes the result to a
file. `login_via_subprocess` is the entry point everything else uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from adobe_rescue import config
from adobe_rescue.auth import AuthError, Session, decode_expiry
from adobe_rescue.util import LOG

#: Loading the files app guarantees a full IMS handshake before we look.
LOGIN_URL = "https://assets.adobe.com/files"
#: Reached only after a successful sign-in.
SUCCESS_HINTS = ("assets.adobe.com", "creativecloud.adobe.com")

#: Visited after sign-in purely to learn each service's client id.
HARVEST_URLS = (
    "https://assets.adobe.com/libraries",
    "https://acrobat.adobe.com/link/home/",
    "https://lightroom.adobe.com/",
)

TOKEN_PREFIX = "adobeid_ims_access_token"

#: Key names seen on the last failed attempt, for the error message.
LOGIN_DIAGNOSTICS: Dict[str, Any] = {"keys": []}
PROFILE_PREFIX = "adobeid_ims_profile"

#: Probe the page for a session, most reliable source first.
#:
#: 1. `adobeIMS.getAccessToken()` — the IMS library Adobe's own web apps use.
#: 2. localStorage / sessionStorage entries that look like IMS tokens.
#:
#: Returns JSON; `items` is capped so a huge storage cannot stall the bridge.
PROBE_JS = """
(function () {
  var r = {via: '', token: '', expire: 0, clientId: '', email: '', items: {}, keys: []};
  try {
    var ims = window.adobeIMS || (window.adobeid && window.adobeid.IMS);
    if (ims && typeof ims.getAccessToken === 'function') {
      var t = ims.getAccessToken();
      if (t && (t.token || t.tokenValue)) {
        r.token = t.token || t.tokenValue;
        r.expire = t.expire || t.expiresAtMilliseconds || 0;
        r.via = 'adobeIMS';
      }
      try { if (ims.adobeIdData) { r.clientId = ims.adobeIdData.client_id || ''; } } catch (e) {}
      try {
        var p = (typeof ims.getProfileFromStorage === 'function') ? ims.getProfileFromStorage() : null;
        if (p && (p.email || p.userId)) { r.email = p.email || p.userId; }
      } catch (e) {}
    }
  } catch (e) {}
  var stores = [];
  try { stores.push(localStorage); } catch (e) {}
  try { stores.push(sessionStorage); } catch (e) {}
  for (var s = 0; s < stores.length; s++) {
    var st = stores[s];
    var n = 0;
    try { n = st.length; } catch (e) { continue; }
    for (var i = 0; i < n; i++) {
      var k = null;
      try { k = st.key(i); } catch (e) { continue; }
      if (!k) { continue; }
      var lk = k.toLowerCase();
      if (lk.indexOf('ims') < 0 && lk.indexOf('token') < 0 && lk.indexOf('adobeid') < 0) { continue; }
      r.keys.push(k);
      if (Object.keys(r.items).length < 40) {
        try { r.items[k] = (st.getItem(k) || '').slice(0, 6000); } catch (e) {}
      }
    }
  }
  return JSON.stringify(r);
})()
"""


#: Record the Creative Cloud web app's own API calls, so we replay its exact
#: URLs and headers instead of guessing them. Installed after sign-in, then
#: the page is reloaded so the app re-issues its startup requests.
CAPTURE_JS = """
(function () {
  if (window.__arCapture) { return 'already'; }
  window.__arCapture = [];
  var keep = function (url, headers) {
    try {
      if (!url || url.indexOf('adobe') < 0) { return; }
      if (window.__arCapture.length > 120) { return; }
      window.__arCapture.push({url: String(url), headers: headers || {}});
    } catch (e) {}
  };
  var flatten = function (h) {
    var out = {};
    try {
      if (!h) { return out; }
      if (typeof h.forEach === 'function' && !Array.isArray(h)) {
        h.forEach(function (v, k) { out[String(k).toLowerCase()] = String(v); });
        return out;
      }
      if (Array.isArray(h)) {
        h.forEach(function (pair) { out[String(pair[0]).toLowerCase()] = String(pair[1]); });
        return out;
      }
      Object.keys(h).forEach(function (k) { out[k.toLowerCase()] = String(h[k]); });
    } catch (e) {}
    return out;
  };
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        var url = (typeof input === 'string') ? input : (input && input.url);
        var hdrs = flatten(init && init.headers);
        if (!Object.keys(hdrs).length && input && input.headers) { hdrs = flatten(input.headers); }
        keep(url, hdrs);
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  }
  var XO = XMLHttpRequest.prototype.open;
  var XS = XMLHttpRequest.prototype.setRequestHeader;
  var XSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__arUrl = u; this.__arHeaders = {};
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { (this.__arHeaders = this.__arHeaders || {})[String(k).toLowerCase()] = String(v); } catch (e) {}
    return XS.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    try { keep(this.__arUrl, this.__arHeaders); } catch (e) {}
    return XSend.apply(this, arguments);
  };
  return 'installed';
})()
"""

#: Hand the recording back and clear it.
READ_CAPTURE_JS = """
(function () {
  try { return JSON.stringify(window.__arCapture || []); }
  catch (e) { return '[]'; }
})()
"""

# --------------------------------------------------------------------------
# Pure parsing — unit tested without a browser
# --------------------------------------------------------------------------
def parse_storage(items: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Pull token, client id, email and expiry out of Adobe's IMS storage.

    Keys look like:
        adobeid_ims_access_token/CreativeCloudWeb/false/AdobeID,openid,...
        adobeid_ims_profile/CreativeCloudWeb/false/AdobeID,openid,...
    """
    if not items or "__error__" in items:
        return None

    token = ""
    client_id = ""
    expires = 0.0

    for key, raw in items.items():
        if not _looks_like_token_key(key):
            continue
        candidate, expiry = _token_from_value(raw)
        if not candidate:
            continue
        parts = key.split("/")
        this_client = parts[1] if len(parts) > 1 else ""
        # Prefer the longest-lived token when the page holds several.
        if expiry >= expires or not token:
            token, client_id, expires = candidate, this_client, expiry

    if not token:
        return None
    if not expires:
        expires = decode_expiry(token)

    return {
        "token": token,
        "api_key": client_id or "CreativeCloudWeb",
        "email": _email_from_profile(items),
        "expires": expires,
    }


def _looks_like_token_key(key: str) -> bool:
    low = key.lower()
    if low.startswith(TOKEN_PREFIX):
        return True
    # Adobe has shipped several key shapes over the years; match the idea,
    # not one spelling. Refresh/device tokens are useless to us, skip them.
    if "refresh_token" in low or "device_token" in low:
        return False
    return "access_token" in low or ("ims" in low and "token" in low)


def _token_from_value(raw: str) -> tuple:
    """IMS stores either a JSON envelope or, rarely, the bare token."""
    raw = (raw or "").strip()
    if not raw:
        return "", 0.0
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return "", 0.0
        token = str(data.get("tokenValue") or data.get("access_token")
                    or data.get("token") or "")
        expire = data.get("expire") or data.get("expires_at") or 0
        if isinstance(expire, dict):
            expire = expire.get("date") or expire.get("value") or 0
        try:
            expire = float(expire or 0)
        except (TypeError, ValueError):
            expire = 0.0
        if expire > 1e11:        # milliseconds
            expire /= 1000.0
        return (token, expire) if token.count(".") >= 2 else ("", 0.0)
    return (raw, 0.0) if raw.count(".") >= 2 else ("", 0.0)


def _email_from_profile(items: Dict[str, str]) -> str:
    for key, raw in items.items():
        if not key.startswith(PROFILE_PREFIX) or not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for field in ("email", "userId", "displayName", "name"):
            value = data.get(field)
            if value:
                return str(value)
    return ""


def parse_probe(result: Any) -> Optional[Dict[str, Any]]:
    """Turn one PROBE_JS result into session fields, or None if not signed in."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if not isinstance(result, dict):
        return None

    token = str(result.get("token") or "")
    if token.count(".") >= 2:
        expires = result.get("expire") or 0
        try:
            expires = float(expires or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires > 1e11:
            expires /= 1000.0
        from adobe_rescue.auth import decode_user
        return {
            "token": token,
            "api_key": (str(result.get("clientId") or "")
                        or decode_user(token).get("client_id", "")
                        or "CreativeCloudWeb"),
            "email": str(result.get("email") or ""),
            "expires": expires or decode_expiry(token),
            "via": str(result.get("via") or "adobeIMS"),
        }

    parsed = parse_storage(result.get("items") or {})
    if parsed:
        parsed.setdefault("via", "storage")
        if result.get("email") and not parsed.get("email"):
            parsed["email"] = str(result["email"])
        return parsed
    return None


def probe_keys(result: Any) -> list:
    """Key NAMES only — safe to show in an error message, never values."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return []
    if not isinstance(result, dict):
        return []
    return [str(k) for k in (result.get("keys") or [])][:25]


def merge_captures(pages: Iterable[Any]) -> list:
    """Flatten every page's capture into one de-duplicated list."""
    merged: list = []
    seen = set()
    for raw in pages:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "")
            headers = entry.get("headers") or {}
            key = (url, str(headers.get("x-api-key", "")) if isinstance(headers, dict) else "")
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return merged


CAPTURE_FILE = "last-capture.json"


def save_capture(raw: Any) -> Optional[Path]:
    """Record the web app's request URLs for inspection.

    Stores method, host, path and x-api-key only — never Authorization,
    never cookies, never bodies. This is what makes a wrong endpoint
    diagnosable without anyone opening DevTools.
    """
    from urllib.parse import urlparse
    from adobe_rescue.auth import _is_adobe

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or not raw:
        return None

    rows = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        if not url or not _is_adobe(url):
            continue
        parsed = urlparse(url)
        if parsed.path.rsplit(".", 1)[-1].lower() in (
                "js", "css", "png", "jpg", "jpeg", "svg", "woff", "woff2", "gif", "ico"):
            continue
        headers = entry.get("headers") or {}
        api_key = ""
        if isinstance(headers, dict):
            api_key = str(headers.get("x-api-key") or "")
        key = (parsed.netloc, parsed.path, parsed.query[:120])
        if key in seen:
            continue
        seen.add(key)
        rows.append({"host": parsed.netloc, "path": parsed.path,
                     "query": parsed.query[:200], "x-api-key": api_key})

    if not rows:
        return None
    path = config.state_dir() / CAPTURE_FILE
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG.info("Web uygulamasının %d isteği kaydedildi: %s", len(rows), path)
    return path


def load_capture() -> list:
    path = config.state_dir() / CAPTURE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


#: Which endpoint groups a given Adobe host serves.
HOST_GROUPS = (
    ("cc-api-storage", ("cloud_files", "cloud_docs")),
    ("cc-libraries", ("libraries",)),
    ("lr.adobe.io", ("lightroom",)),
    ("dc-api", ("acrobat",)),
    ("acrobat.adobe.com", ("acrobat",)),
    ("adobeprojectm", ("express",)),
    ("typekit", ("fonts",)),
    ("fonts.adobe.com", ("fonts",)),
)


def groups_for_url(url: str) -> tuple:
    low = url.lower()
    for needle, groups in HOST_GROUPS:
        if needle in low:
            return groups
    return ()


#: Headers worth replaying verbatim; anything else is noise or per-request.
REPLAY_HEADERS = ("x-api-key", "x-product", "x-product-location", "x-adobe-quota")


def digest_capture(entries: Any) -> Dict[str, Any]:
    """Turn recorded web-app requests into api key, headers and endpoints.

    This is what removes the guesswork behind HTTP 403: the api key we send
    is the one the Creative Cloud web app itself sent with the same token.
    """
    from adobe_rescue.auth import _classify_endpoint, _is_adobe

    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except ValueError:
            return {}
    if not isinstance(entries, list):
        return {}

    api_key = ""
    extra: Dict[str, str] = {}
    discovered: Dict[str, str] = {}
    seen = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        if not url or not _is_adobe(url):
            continue
        raw_headers = entry.get("headers")
        headers = ({str(k).lower(): str(v) for k, v in raw_headers.items()}
                   if isinstance(raw_headers, dict) else {})
        seen += 1
        key = headers.get("x-api-key")
        if key:
            # Each Adobe service wants its own client id, so record the key
            # against the service it was actually used on rather than
            # globally — Lightroom's key is not Creative Cloud's.
            for group in groups_for_url(url):
                discovered.setdefault(f"{group}.api_key", key)
            if not api_key and "cc-api-storage" in url:
                api_key = key
            elif not api_key:
                api_key = key
        for name in REPLAY_HEADERS:
            if name in headers and name != "x-api-key":
                extra.setdefault(name, headers[name])
        _classify_endpoint(url, discovered)

    if not seen:
        return {}
    return {"api_key": api_key, "extra_headers": extra,
            "discovered": discovered, "requests": seen}


def session_from_storage(items: Dict[str, str]) -> Optional[Session]:
    parsed = parse_storage(items)
    if not parsed:
        return None
    return Session(
        token=parsed["token"],
        api_key=parsed["api_key"],
        email=parsed["email"],
        expires=parsed["expires"],
    )


# --------------------------------------------------------------------------
# The webview window (runs in a child process)
# --------------------------------------------------------------------------
def webview_available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def run_login_window(timeout: float = 420.0) -> Optional[Session]:
    """Open the sign-in window and wait for a token. Main thread only."""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise AuthError(
            "Giriş penceresi açılamadı (webview bileşeni yok). "
            "Gelişmiş bölümünden jeton/HAR ile giriş yapabilirsiniz."
        ) from exc

    found: Dict[str, Any] = {"session": None, "keys": []}
    window = webview.create_window(
        "Adobe'ye giriş yapın", LOGIN_URL, width=1024, height=800)

    last_keys: Dict[str, list] = {"keys": []}

    def read_once(win) -> Optional[Session]:
        """One probe. Never raises: during navigation this throws routinely."""
        try:
            raw = win.evaluate_js(PROBE_JS)
        except Exception as exc:
            LOG.debug("giriş sayfası henüz okunamıyor: %s", exc)
            return None
        if not raw:
            return None
        keys = probe_keys(raw)
        if keys:
            last_keys["keys"] = keys
        parsed = parse_probe(raw)
        if not parsed:
            return None
        session = Session(token=parsed["token"], api_key=parsed["api_key"],
                          email=parsed["email"], expires=parsed["expires"])
        if session.is_expired():
            return None
        LOG.debug("oturum bulundu (%s)", parsed.get("via", "?"))
        return session

    def on_closing() -> None:
        """User closed the window — take one last look before it goes."""
        if found["session"] is None:
            found["session"] = read_once(window)

    def on_loaded() -> None:
        """Re-arm the request hooks after every navigation.

        Injecting them once and then reloading (which is what the previous
        version did) threw them away: a reload builds a fresh JS context.
        """
        try:
            window.evaluate_js(CAPTURE_JS)
        except Exception as exc:  # pragma: no cover - page may be mid-flight
            LOG.debug("kayıt kancası kurulamadı: %s", exc)

    for event_name, handler in (("closing", on_closing), ("loaded", on_loaded)):
        try:
            getattr(window.events, event_name).__iadd__(handler)
        except Exception:  # pragma: no cover - older pywebview
            pass

    def learn_requests(win, session: Session) -> None:
        """Collect the URLs and headers the Creative Cloud app itself uses."""
        try:
            win.evaluate_js(CAPTURE_JS)          # no-op if already installed
        except Exception as exc:
            LOG.debug("istek kaydı başlatılamadı: %s", exc)

        deadline = time.time() + 30.0
        best: Dict[str, Any] = {}
        collected: List[Any] = []     # every page's raw capture, merged later
        reloaded = False
        while time.time() < deadline:
            time.sleep(3.0)
            try:
                raw = win.evaluate_js(READ_CAPTURE_JS)
            except Exception:
                continue           # page navigating
            collected.append(raw)
            digest = digest_capture(raw)
            if digest.get("requests", 0) > best.get("requests", 0):
                best = digest
            if best.get("api_key") and len(best.get("discovered") or {}) >= 2:
                break
            if not best and not reloaded and time.time() > deadline - 20:
                # Nothing seen yet: the app finished its calls before we
                # hooked in. Reload — the loaded handler re-arms the hooks
                # early enough to catch the startup requests this time.
                reloaded = True
                try:
                    win.evaluate_js("location.reload()")
                except Exception:
                    pass
        # The files page never calls Acrobat, Lightroom or Libraries, so their
        # client ids stay unknown unless we visit those apps too. Each
        # navigation re-arms the hooks via the loaded event.
        for url in HARVEST_URLS:
            if time.time() > deadline + 45:
                break
            try:
                win.evaluate_js(f"location.assign({json.dumps(url)})")
            except Exception:
                break
            time.sleep(7.0)
            try:
                raw = win.evaluate_js(READ_CAPTURE_JS)
            except Exception:
                continue
            collected.append(raw)
            digest = digest_capture(raw)
            if digest.get("requests", 0) > best.get("requests", 0):
                best = digest
            # Each app has its own client id, so merge rather than replace:
            # keeping only the richest page lost Libraries and Acrobat.
            session.discovered.update({k: v for k, v in (digest.get("discovered") or {}).items()
                                       if k not in session.discovered})

        save_capture(merge_captures(collected))
        if not best:
            LOG.debug("istek kaydı boş döndü")
            return
        if best.get("api_key"):
            session.api_key = best["api_key"]
        session.extra_headers.update(best.get("extra_headers") or {})
        session.discovered.update(best.get("discovered") or {})
        LOG.debug("web uygulamasından öğrenildi: api_key=%s, %d uç nokta, %d istek",
                  session.api_key, len(session.discovered), best.get("requests", 0))

    def poll(win) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2.0)
            if found["session"] is not None:
                break
            session = read_once(win)
            if session is None:
                continue
            learn_requests(win, session)
            found["session"] = session
            break
        else:
            LOG.warning("Giriş zaman aşımına uğradı.")
        found["keys"] = last_keys["keys"]
        try:
            win.destroy()
        except Exception:  # pragma: no cover
            pass

    # private_mode=False keeps cookies, so a second run does not ask again.
    webview.start(poll, window, private_mode=False)
    LOGIN_DIAGNOSTICS["keys"] = found.get("keys") or []
    return found["session"]


# --------------------------------------------------------------------------
# Parent-side entry point
# --------------------------------------------------------------------------
def _worker_command(out_path: Path) -> list:
    """How to re-invoke ourselves to show the login window."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "login", "--browser-worker", "--out", str(out_path)]
    return [sys.executable, "-m", "adobe_rescue", "login",
            "--browser-worker", "--out", str(out_path)]


def login_via_subprocess(timeout: float = 480.0) -> Session:
    """Show the login window in a child process and return the session."""
    handle, raw_path = tempfile.mkstemp(prefix="adobe-login-", suffix=".json")
    os.close(handle)
    out_path = Path(raw_path)
    out_path.unlink(missing_ok=True)

    command = _worker_command(out_path)
    LOG.debug("giriş penceresi başlatılıyor: %s", command)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(command, timeout=timeout, capture_output=True,
                              creationflags=creationflags)
    except subprocess.TimeoutExpired as exc:
        out_path.unlink(missing_ok=True)
        raise AuthError("Giriş penceresi zaman aşımına uğradı.") from exc
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        raise AuthError(f"Giriş penceresi başlatılamadı: {exc}") from exc

    if not out_path.exists():
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise AuthError(
            "Giriş tamamlanmadı. Pencereyi kapattıysanız tekrar deneyin."
            + (f"\n{detail[-400:]}" if detail else ""))

    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    if not data.get("token"):
        if data.get("error") and data["error"] != "no-session":
            raise AuthError(str(data["error"]))
        keys = data.get("keys") or []
        detail = ("\n\nSayfada bulunan anahtarlar:\n  " + "\n  ".join(keys[:12])
                  if keys else "")
        raise AuthError(
            "Giriş yapıldı ama oturum anahtarı okunamadı. Gelişmiş bölümünden "
            "jeton ile deneyebilirsiniz." + detail)
    return Session(token=data["token"], api_key=data.get("api_key") or "CreativeCloudWeb",
                   email=data.get("email", ""), expires=float(data.get("expires", 0) or 0),
                   extra_headers=data.get("extra_headers") or {},
                   discovered=data.get("discovered") or {})


def worker_main(out_path: Path) -> int:
    """Child-process entry point: show the window, write the result, exit.

    A result file is always written — on failure it carries the storage key
    NAMES we saw (never values), which is what tells us how to adapt if Adobe
    moves the session again.
    """
    try:
        session = run_login_window()
    except AuthError as exc:
        out_path.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        return 1

    if not session:
        out_path.write_text(json.dumps({
            "error": "no-session",
            "keys": LOGIN_DIAGNOSTICS.get("keys") or [],
        }), encoding="utf-8")
        return 1

    out_path.write_text(json.dumps({
        "token": session.token,
        "api_key": session.api_key,
        "email": session.email,
        "expires": session.expires,
        "extra_headers": session.extra_headers,
        "discovered": session.discovered,
    }), encoding="utf-8")
    return 0
