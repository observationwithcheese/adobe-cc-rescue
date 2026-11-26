"""Ground-truth probe: which endpoint + api key combination does Adobe accept?

Run on a machine that has already signed in (`python -m adobe_rescue login`).
Prints an HTTP status for every candidate combination so the working one can
become the built-in default.

Prints NO credentials: never the token, never cookies. Response bodies are
truncated to the error title Adobe returns.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from adobe_rescue.auth import Session, decode_user  # noqa: E402

TIMEOUT = 20

STORAGE_URLS = [
    "https://cc-api-storage.adobe.io/content/storage/id/root",
    "https://cc-api-storage.adobe.io/content/storage/v2/id/root",
    "https://cc-api-storage.adobe.io/id/root",
    "https://cc-api-storage.adobe.io/content/storage/repository",
    "https://platform-cs.adobe.io/content/storage/id/root",
    "https://cc-collab.adobe.io/content/storage/id/root",
]

OTHER_URLS = {
    "acrobat": ["https://dc-api.adobe.io/assets",
                "https://dc-api.adobe.io/discovery/"],
    "lightroom": ["https://lr.adobe.io/v2/account",
                  "https://lr.adobe.io/v2/catalog"],
    "libraries": ["https://cc-libraries.adobe.io/api/v1/libraries"],
}

EXTRA_KEYS = [
    "CreativeCloudWeb", "cc-web-storage", "CCHomeWeb", "cc_files",
    "creativecloud-web", "AdobeCCWeb", "cc-storage-web", "ccweb",
    "dc-web-app", "LightroomWeb",
]


def probe(url: str, token: str, api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "adobe-cc-rescue-probe/1.0",
    }
    if api_key:
        headers["x-api-key"] = api_key
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT,
                                params={"limit": 1})
    except requests.RequestException as exc:
        return f"NET  {type(exc).__name__}"
    body = ""
    try:
        text = (response.text or "").replace("\n", " ")[:120]
        body = text
    except Exception:
        pass
    return f"{response.status_code}  {body}"


def main() -> int:
    session = Session.load()
    if not session.token:
        print("Oturum yok. Önce: python -m adobe_rescue login")
        return 1

    info = decode_user(session.token)
    print("=" * 78)
    print(f"client_id (jetondan) : {info.get('client_id') or '(yok)'}")
    print(f"kayıtlı api_key      : {session.api_key}")
    print(f"e-posta              : {session.email or info.get('email') or '?'}")
    print(f"süresi doldu mu      : {session.is_expired()}")
    print(f"öğrenilmiş ayarlar   : {session.discovered or '(yok)'}")
    print(f"ek başlıklar         : {list(session.extra_headers) or '(yok)'}")
    print("=" * 78)

    keys = []
    for key in [info.get("client_id", ""), session.api_key, *EXTRA_KEYS, ""]:
        if key not in keys:
            keys.append(key)

    print("\n--- STORAGE (bulut dosyaları) ---")
    for url in STORAGE_URLS:
        print(f"\n{url}")
        for key in keys:
            label = key or "(x-api-key YOK)"
            print(f"   {label:<22} -> {probe(url, session.token, key)}")

    print("\n--- DİĞER SERVİSLER (varsayılan anahtarlarla) ---")
    for group, urls in OTHER_URLS.items():
        default = session.endpoint(group, "api_key")
        for url in urls:
            print(f"{group:<10} {url}")
            for key in {default, info.get("client_id", ""), "CreativeCloudWeb"}:
                if not key:
                    continue
                print(f"   {key:<22} -> {probe(url, session.token, key)}")
    print("\nBitti. 200 dönen satırları paylaşın.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
