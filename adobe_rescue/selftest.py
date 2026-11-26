"""Post-login connection check.

Finding out that Adobe refuses the session should take two seconds after
signing in, not twenty minutes into a backup. This makes one cheap, read-only
request and reports plainly what came back.
"""

from __future__ import annotations

from typing import Tuple

from adobe_rescue.auth import AuthError, Session
from adobe_rescue.config import Settings
from adobe_rescue.http import Client, DownloadError
from adobe_rescue.util import LOG


def check_cloud_access(session: Session, timeout: float = 25.0) -> Tuple[bool, str]:
    """Return (ok, human message). Never raises."""
    if not session.token:
        return False, "Giriş yapılmadı."
    if session.is_expired():
        return False, "Oturumun süresi dolmuş — tekrar giriş yapın."

    settings = Settings(timeout=timeout, retries=1)
    client = Client(session, settings)
    url = session.endpoint("cloud_files", "list_root")
    api_key = session.endpoint("cloud_files", "api_key")
    try:
        data = client.get_json(url, api_key=api_key, params={"limit": 1})
    except AuthError as exc:
        return False, str(exc)
    except DownloadError as exc:
        return False, (f"Bulut adresine ulaşılamadı: {exc}\n"
                       "Uç noktalar değişmiş olabilir — Gelişmiş > HAR ile yenileyin.")
    except Exception as exc:                      # network, DNS, proxy...
        return False, f"Bağlantı hatası: {exc}"
    finally:
        client.close()

    count = 0
    if isinstance(data, dict):
        count = len(data.get("children") or data.get("items") or [])
    LOG.debug("bağlantı testi tamam (%s, api_key=%s)", url, api_key)
    return True, (f"Bulut erişimi doğrulandı (api_key: {api_key})"
                  + (f" · kökte en az {count} öğe" if count else ""))
