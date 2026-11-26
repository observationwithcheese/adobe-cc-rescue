"""HTTP engine: retries, backoff, throttling, resumable range downloads."""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter

from adobe_rescue.auth import ApiKeyError, AuthError, Session as AdobeSession
from adobe_rescue.config import Settings
from adobe_rescue.util import LOG, human_size

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(RuntimeError):
    pass


class Throttle:
    """Simple global request pacer shared by all worker threads."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.min_interval:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.min_interval


class Client:
    def __init__(self, session: AdobeSession, settings: Settings):
        self.adobe = session
        self.settings = settings
        self.throttle = Throttle(settings.rate_limit)
        self.http = requests.Session()
        adapter = HTTPAdapter(pool_connections=settings.workers * 2,
                              pool_maxsize=settings.workers * 2,
                              max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    # ------------------------------------------------------------- plumbing
    def _sleep_for(self, attempt: int, response: Optional[requests.Response]) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), 120.0)
                except ValueError:
                    pass
        return min(2 ** attempt + random.uniform(0, 0.75), 60.0)

    def request(self, method: str, url: str, *, api_key: Optional[str] = None,
                stream: bool = False, **kwargs: Any) -> requests.Response:
        headers = self.adobe.headers(api_key, url=url)
        headers.update(kwargs.pop("headers", {}) or {})
        last_exc: Optional[Exception] = None
        for attempt in range(self.settings.retries + 1):
            self.throttle.wait()
            try:
                response = self.http.request(
                    method, url, headers=headers, stream=stream,
                    timeout=self.settings.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == self.settings.retries:
                    break
                wait = self._sleep_for(attempt, None)
                LOG.debug("  ağ hatası (%s), %.1fs sonra tekrar", exc, wait)
                time.sleep(wait)
                continue

            if response.status_code in (401, 403) and not self.adobe.trusted(url):
                # A presigned link carries its own authorisation and expires;
                # this is a stale URL, not a broken session. Retrying the
                # item re-runs the job and mints a fresh one.
                response.close()
                raise DownloadError(
                    "İndirme bağlantısının süresi dolmuş (HTTP %d). "
                    "Aynı komutu tekrar çalıştırın; bağlantı yenilenir."
                    % response.status_code)
            if response.status_code in (401, 403):
                detail = ""
                try:
                    detail = (response.text or "")[:400].replace("\n", " ").strip()
                except Exception:
                    pass
                response.close()
                if response.status_code == 401:
                    raise AuthError(
                        "Oturumun süresi doldu (HTTP 401). Tekrar giriş yapın."
                        + (f"\nAdobe: {detail}" if detail else ""))
                # 403: the token is fine, Adobe refused *this* request — nearly
                # always an x-api-key that does not match the token's client.
                raise ApiKeyError(
                    "Adobe isteği reddetti (HTTP 403). Oturum geçerli ama bu API "
                    "anahtarıyla erişim yok (x-api-key: %s)."
                    "\nGiriş penceresini tekrar açıp birkaç saniye bekleyin: uygulama "
                    "Adobe'nin kendi isteklerinden doğru anahtarı öğrenir."
                    % (api_key or self.adobe.api_key)
                    + (f"\nAdobe: {detail}" if detail else ""))
            if response.status_code in RETRYABLE and attempt < self.settings.retries:
                wait = self._sleep_for(attempt, response)
                LOG.debug("  HTTP %d, %.1fs sonra tekrar: %s", response.status_code, wait, url)
                response.close()
                time.sleep(wait)
                continue
            return response
        raise DownloadError(f"İstek başarısız: {url} ({last_exc})")

    def get_json(self, url: str, *, api_key: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        response = self.request("GET", url, api_key=api_key, params=params)
        if response.status_code == 404:
            raise DownloadError(f"Uç nokta bulunamadı (404): {url}")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise DownloadError(f"Beklenmeyen yanıt (JSON değil): {url}") from exc

    def paginate(self, url: str, *, api_key: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None,
                 items_key: str = "children",
                 next_key: str = "_links") -> Iterator[Dict[str, Any]]:
        """Walk Adobe's HAL-ish pagination (`_links.next.href`) safely."""
        seen_urls = set()
        next_url: Optional[str] = url
        query = dict(params or {})
        while next_url and next_url not in seen_urls:
            seen_urls.add(next_url)
            data = self.get_json(next_url, api_key=api_key, params=query)
            query = {}  # only the first call carries params; next href is complete
            if isinstance(data, list):
                for item in data:
                    yield item
                return
            for item in data.get(items_key) or data.get("items") or []:
                yield item
            links = data.get(next_key) or {}
            nxt = links.get("next") if isinstance(links, dict) else None
            next_url = nxt.get("href") if isinstance(nxt, dict) else (nxt if isinstance(nxt, str) else None)

    # ------------------------------------------------------------ downloads
    def download(self, url: str, target: Path, *, api_key: Optional[str] = None,
                 expected_size: int = 0,
                 on_progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Download to `target`, resuming a partial `.part` file when possible."""
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        existing = part.stat().st_size if part.exists() else 0

        headers: Dict[str, str] = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"

        response = self.request("GET", url, api_key=api_key, stream=True, headers=headers)
        if response.status_code == 416:  # already complete
            response.close()
            existing_total = existing
            part.replace(target)
            return existing_total
        if response.status_code == 404:
            response.close()
            raise DownloadError("Dosya bulunamadı (404)")
        response.raise_for_status()

        mode = "ab"
        if response.status_code != 206:    # server ignored our Range → restart
            existing = 0
            mode = "wb"

        total = expected_size
        length = response.headers.get("Content-Length")
        if length and length.isdigit():
            total = existing + int(length)

        written = existing
        try:
            with part.open(mode) as fh:
                for chunk in response.iter_content(chunk_size=self.settings.chunk_size):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
        finally:
            response.close()

        if total and written < total:
            raise DownloadError(
                f"Eksik indirme: {human_size(written)} / {human_size(total)} — tekrar çalıştırın")
        part.replace(target)
        return written

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:  # pragma: no cover
            pass
