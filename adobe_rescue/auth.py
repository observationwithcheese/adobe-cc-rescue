"""Authentication.

Adobe has no public bulk-export API for personal cloud storage, so the tool
borrows the credentials your own browser already uses:

1. `--token`  : paste the Bearer access token from DevTools.
2. `--har`    : export a .har from DevTools while browsing assets.adobe.com and
                let the tool extract the token, api key and the exact endpoints
                your tenant uses. This survives Adobe changing their URLs.
3. env var    : ADOBE_RESCUE_TOKEN

Credentials are stored in the per-user state dir (0600 where the OS supports
it), never inside the backup folder, and never logged.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from adobe_rescue import config
from adobe_rescue.util import LOG

SESSION_FILE = "session.json"


class AuthError(RuntimeError):
    """The session itself is unusable — nothing can continue."""


class ApiKeyError(AuthError):
    """This one service refused our client id; other sources are fine.

    Adobe issues a separate client id per service. Rejecting one (403
    ErrInvalidAPIKey) says nothing about the session, so the run continues
    with that source marked failed.
    """


@dataclass
class Session:
    token: str = ""
    api_key: str = "CreativeCloudWeb"
    user_id: str = ""
    email: str = ""
    expires: float = 0.0
    extra_headers: Dict[str, str] = field(default_factory=dict)
    discovered: Dict[str, str] = field(default_factory=dict)  # endpoint overrides

    # --------------------------------------------------------------- state
    @property
    def path(self) -> Path:
        return config.state_dir() / SESSION_FILE

    def headers(self, api_key: Optional[str] = None,
                url: Optional[str] = None) -> Dict[str, str]:
        """Headers for one request.

        `url` matters: download and rendition links are frequently presigned
        URLs on CDN or S3 hosts, which carry their own authorisation in the
        query string. Sending our bearer token there would hand the account
        token to a third party, so credentials are attached only for Adobe
        hosts. Presigned links are fetched bare.
        """
        if not self.token:
            raise AuthError("Oturum yok. Önce `adobe-rescue login` çalıştırın.")
        if url is not None and not self.trusted(url):
            return {"Accept": "*/*",
                    "User-Agent": "adobe-cc-rescue/1.0 (+backup before tenant migration)"}
        head = {
            "Authorization": f"Bearer {self.token}",
            "x-api-key": api_key or self.api_key,
            # Adobe's storage APIs answer 406 Not Acceptable to
            # "application/json" and 200 to "*/*" — they serve vendor types
            # such as application/vnd.adobecloud.index+json. Confirmed live.
            "Accept": "*/*",
            "User-Agent": "adobe-cc-rescue/1.0 (+backup before tenant migration)",
        }
        head.update(self.extra_headers)
        return _safe_headers(head)

    def trusted(self, url: str) -> bool:
        """May this URL receive the account's credentials?

        Adobe hosts, plus any host we learned from the signed-in session's
        own traffic (those were already filtered to Adobe when captured).
        Everything else — presigned CDN and S3 download links above all —
        is fetched without credentials.
        """
        if _is_adobe(url):
            return True
        host = urlparse(url).netloc.lower()
        if not host:
            return False
        for value in self.discovered.values():
            if isinstance(value, str) and value.startswith("http"):
                if urlparse(value).netloc.lower() == host:
                    return True
        return False

    def endpoint(self, group: str, key: str) -> str:
        """Learned values win over the built-in defaults.

        Endpoints and api keys recorded from the Creative Cloud web app's own
        requests are authoritative: they are what Adobe accepted for *this*
        account, with this token, today.
        """
        override = self.discovered.get(f"{group}.{key}")
        if override:
            return override
        return config.ENDPOINTS[group][key]

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires - time.time()) if self.expires else 0.0

    def is_expired(self) -> bool:
        return bool(self.expires) and self.expires <= time.time() + 30

    def save(self) -> Path:
        payload = {
            "token": self.token,
            "api_key": self.api_key,
            "user_id": self.user_id,
            "email": self.email,
            "expires": self.expires,
            "extra_headers": self.extra_headers,
            "discovered": self.discovered,
        }
        path = self.path
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return path

    @classmethod
    def load(cls) -> "Session":
        env_token = os.environ.get("ADOBE_RESCUE_TOKEN", "").strip()
        path = config.state_dir() / SESSION_FILE
        session = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = cls(
                    token=data.get("token", ""),
                    api_key=data.get("api_key", "CreativeCloudWeb"),
                    user_id=data.get("user_id", ""),
                    email=data.get("email", ""),
                    expires=float(data.get("expires", 0) or 0),
                    extra_headers=data.get("extra_headers", {}) or {},
                    discovered=data.get("discovered", {}) or {},
                )
            except (OSError, ValueError) as exc:
                LOG.warning("Kayıtlı oturum okunamadı: %s", exc)
        if env_token:
            session.token = env_token
            session.expires = decode_expiry(env_token) or session.expires
        return session

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ helpers
def _safe_headers(head: Dict[str, str]) -> Dict[str, str]:
    """Drop header values http.client cannot put on the wire.

    Everything here is latin-1 encoded by the stdlib; a stray character
    raises UnicodeEncodeError deep inside the socket layer, which surfaces as
    an unreadable error instead of a failed request.
    """
    clean: Dict[str, str] = {}
    for name, value in head.items():
        text = str(value)
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            if name.lower() == "authorization":
                raise AuthError(
                    "Oturum jetonu bozuk (gönderilemeyen karakter içeriyor). "
                    "Büyük ihtimalle kopyalanırken kısaltılmış — "
                    "'Adobe ile giriş yap' ile yeniden giriş yapın.")
            LOG.warning("Başlık atlandı (gönderilemiyor): %s", name)
            continue
        clean[name] = text
    return clean


def decode_expiry(token: str) -> float:
    """Read `exp`/`created_at`+`expires_in` out of an Adobe JWT without verifying it."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return 0.0
    if "exp" in data:
        exp = float(data["exp"])
        return exp / 1000.0 if exp > 1e11 else exp
    created = float(data.get("created_at", 0) or 0) / 1000.0
    ttl = float(data.get("expires_in", 0) or 0) / 1000.0
    return created + ttl if created and ttl else 0.0


def decode_user(token: str) -> Dict[str, str]:
    try:
        parts = token.split(".")
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return {}
    return {
        "user_id": str(data.get("user_id", "") or data.get("sub", "")),
        "email": str(data.get("email", "")),
        # Adobe refuses a request whose x-api-key is not the client the token
        # was minted for (403 ErrInvalidAPIKey). The token says which.
        "client_id": str(data.get("client_id", "") or data.get("aud_client_id", "")),
    }


def from_token(token: str) -> Session:
    token = token.strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token.count(".") < 2:
        raise AuthError(
            "Bu bir Adobe erişim jetonuna benzemiyor. DevTools > Network > herhangi bir "
            "adobe.io isteği > Request Headers > Authorization: Bearer <JETON> kısmını kopyalayın."
        )
    check_token_text(token)
    info = decode_user(token)
    return Session(
        token=token,
        api_key=info.get("client_id") or "CreativeCloudWeb",
        user_id=info.get("user_id", ""),
        email=info.get("email", ""),
        expires=decode_expiry(token),
    )


def check_token_text(token: str) -> None:
    """Catch a token that was copied out of the DevTools *display*.

    Chrome shortens long header values with an ellipsis, so copying from the
    Headers panel yields 512 characters and a '…'. That token then breaks
    every request with a latin-1 encoding error rather than an HTTP status,
    which is impossible to read as "your token is cut off".
    """
    bad = [ch for ch in token if ord(ch) > 127]
    if not bad:
        return
    if "\u2026" in token or "…" in token:
        raise AuthError(
            "Jeton kopyalanırken kısaltılmış (içinde '…' var). Chrome, uzun "
            "başlıkları ekranda kısaltır. Satıra sağ tıklayıp 'Copy value' "
            "kullanın ya da daha kolayı: 'Adobe ile giriş yap' düğmesi.")
    raise AuthError(
        "Jeton geçersiz karakter içeriyor (%s). Eksik kopyalanmış olabilir."
        % "".join(sorted(set(bad))[:5]))


# --------------------------------------------------------------------- HAR
def _is_adobe(url: str) -> bool:
    """Is this host genuinely Adobe's?

    Must match on a label boundary: a bare endswith("adobe.com") also
    accepts notadobe.com and evil-adobe.com, which would hand them the
    account token.
    """
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in config.ADOBE_HOSTS)


def from_har(har_path: Path) -> Session:
    """Extract token, api-key and live endpoints from a DevTools HAR export."""
    path = Path(har_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"HAR dosyası okunamadı: {exc}") from exc

    entries: List[Dict[str, Any]] = raw.get("log", {}).get("entries", [])
    if not entries:
        raise AuthError("HAR dosyasında istek yok. Kaydı yaparken sayfayı yenilediğinizden emin olun.")

    token = ""
    api_key = ""
    extra: Dict[str, str] = {}
    discovered: Dict[str, str] = {}
    best_exp = 0.0

    interesting_headers = {"x-api-key", "x-product", "x-product-location", "x-adobe-quota"}

    for entry in entries:
        request = entry.get("request", {})
        url = request.get("url", "")
        if not _is_adobe(url):
            continue
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in request.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidate = auth[7:].strip()
            exp = decode_expiry(candidate)
            if exp >= best_exp:          # keep the freshest token we saw
                token, best_exp = candidate, exp
                api_key = headers.get("x-api-key", api_key)
        for name in interesting_headers:
            if name in headers and name != "x-api-key":
                extra[name] = headers[name]
        _classify_endpoint(url, discovered)

    if not token:
        raise AuthError(
            "HAR içinde Authorization: Bearer başlığı bulunamadı. DevTools'u açıp "
            "assets.adobe.com sayfasını YENİLEYİP kaydı öyle alın."
        )

    check_token_text(token)
    info = decode_user(token)
    session = Session(
        token=token,
        api_key=api_key or info.get("client_id") or "CreativeCloudWeb",
        user_id=info.get("user_id", ""),
        email=info.get("email", ""),
        expires=best_exp,
        extra_headers=extra,
        discovered=discovered,
    )
    LOG.info("HAR'dan %d uç nokta ve geçerli bir jeton çıkarıldı.", len(discovered))
    return session


def _classify_endpoint(url: str, out: Dict[str, str]) -> None:
    """Map observed URLs onto the endpoint slots we care about."""
    low = url.lower()
    base = url.split("?")[0]
    if "cc-api-storage" in low and "/content/storage/" in low and "cloud_files.list_root" not in out:
        out["cloud_files.list_root"] = base
    elif "cc-libraries" in low and "libraries" in low and "libraries.list" not in out:
        out["libraries.list"] = base
    elif "lr.adobe.io" in low and "/catalog" in low and "lightroom.catalog" not in out:
        out["lightroom.catalog"] = base
    elif ("dc-api" in low or "acrobat.adobe.com" in low) and "assets" in low and "acrobat.assets" not in out:
        out["acrobat.assets"] = base
    elif "adobeprojectm" in low and "projects" in low and "express.list" not in out:
        out["express.list"] = base
