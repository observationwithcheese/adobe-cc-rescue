"""Configuration: endpoints, platform paths, deadline, runtime settings.

Everything Adobe-specific that can drift over time lives here so it can be
patched in one place (or overridden from a JSON file / HAR capture) without
touching the download engine.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_NAME = "adobe-cc-rescue"

# --------------------------------------------------------------------------
# Migration deadline (local time). Set by the admin announcement:
#   "Cumartesi akşam 23:00'e kadar yedeklerinizi alın."
# Override with ADOBE_RESCUE_DEADLINE=YYYY-MM-DDTHH:MM
# --------------------------------------------------------------------------
DEFAULT_DEADLINE = "2026-11-28T23:00"


def deadline() -> Optional[datetime]:
    raw = os.environ.get("ADOBE_RESCUE_DEADLINE", DEFAULT_DEADLINE).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Adobe web endpoints.
#
# These are the undocumented endpoints the Creative Cloud web apps call from
# the browser. They are NOT a public API and Adobe changes them without
# notice. If a source starts returning 404/403, re-capture them with:
#     adobe-rescue login --har path/to/adobe.har
# which rewrites the discovered values into the session file.
# --------------------------------------------------------------------------
ENDPOINTS: Dict[str, Dict[str, str]] = {
    "cloud_files": {
        # Repository API used by https://assets.adobe.com / adobe.com/files
        "root": "https://cc-api-storage.adobe.io/",
        "list": "https://cc-api-storage.adobe.io/id/{asset_id}",
        # Confirmed live: this legacy folder listing returns 200 (and is empty
        # on newer accounts). The real content is reached through platform.py.
        "list_root": "https://cc-api-storage.adobe.io/files?limit=100&order=desc&orderby=modified",
        "index": "https://platform-cs.adobe.io/index",
        "download": "https://cc-api-storage.adobe.io/id/{asset_id}",
        "discovery": "https://cc-api-storage.adobe.io/",
        # Confirmed against a live account: the Creative Cloud web app's token
        # carries client_id CCXWeb1, and cc-api-storage rejects every other
        # key with 403 ErrInvalidAPIKey.
        "api_key": "CCXWeb1",
    },
    "cloud_docs": {
        # Cloud documents (.psdc/.aic) — listed by the same repository API but
        # need a rendition/export request to come down as a flat PSD/AI.
        "export": "https://cc-api-storage.adobe.io/id/{asset_id}/export",
        "rendition": "https://cc-api-storage.adobe.io/id/{asset_id}/:rendition",
        "api_key": "CCXWeb1",
    },
    "acrobat": {
        "list": "https://dc-api.adobe.io/discovery/",
        "assets": "https://dc-api.adobe.io/assets",
        "download": "https://dc-api.adobe.io/assets/{asset_id}",
        "api_key": "dc-web-app",
    },
    "lightroom": {
        "account": "https://lr.adobe.io/v2/account",
        "catalog": "https://lr.adobe.io/v2/catalog",
        "assets": "https://lr.adobe.io/v2/catalogs/{catalog_id}/assets",
        "albums": "https://lr.adobe.io/v2/catalogs/{catalog_id}/albums",
        "api_key": "LightroomWeb",
    },
    "libraries": {
        "list": "https://cc-libraries.adobe.io/api/v1/libraries",
        "api_key": "CreativeCloudWeb",
    },
    "express": {
        "list": "https://projects.adobeprojectm.com/api/v2/projects",
        "api_key": "ExpressWeb",
    },
    "fonts": {
        "active": "https://typekit.com/api/v1/json/kits",
        "sync": "https://fonts.adobe.com/api/sync_fonts",
        "api_key": "CreativeCloudWeb",
    },
}

ADOBE_HOSTS = (
    "adobe.io",
    "adobe.com",
    "adobeprojectm.com",
    "typekit.com",
    "typekit.net",
)


# --------------------------------------------------------------------------
# Local filesystem locations per OS.
# --------------------------------------------------------------------------
def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def home() -> Path:
    return Path.home()


def state_dir() -> Path:
    """Where we keep the session + resume state (never inside the backup)."""
    if is_windows():
        base = Path(os.environ.get("APPDATA", home() / "AppData" / "Roaming"))
    elif is_macos():
        base = home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", home() / ".local" / "state"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    return home() / "Desktop" / f"Adobe-Backup-{stamp}"


def creative_cloud_sync_dirs() -> List[Path]:
    """Candidate local Creative Cloud Files sync folders."""
    candidates = [
        home() / "Creative Cloud Files",
        home() / "Creative Cloud Files Personal Account",
    ]
    # Business/enterprise profiles append the org name, e.g.
    # "Creative Cloud Files Acme Inc (12345)".
    try:
        for child in home().iterdir():
            if child.is_dir() and child.name.startswith("Creative Cloud Files"):
                candidates.append(child)
    except OSError:
        pass
    seen: Dict[str, Path] = {}
    for c in candidates:
        if c.exists():
            seen[str(c)] = c
    return list(seen.values())


def preset_locations() -> Dict[str, List[Path]]:
    """App preset/settings folders worth copying (brushes, actions, swatches...).

    Keys are the label used in the backup tree; values are candidate paths.
    Version-numbered folders are globbed by the caller.
    """
    h = home()
    if is_windows():
        appdata = Path(os.environ.get("APPDATA", h / "AppData" / "Roaming"))
        docs = h / "Documents"
        return {
            "photoshop": [appdata / "Adobe" / "Adobe Photoshop*"],
            "illustrator": [appdata / "Adobe" / "Adobe Illustrator*"],
            "indesign": [appdata / "Adobe" / "InDesign" / "Version*"],
            "premiere": [docs / "Adobe" / "Premiere Pro"],
            "after-effects": [docs / "Adobe" / "After Effects*"],
            "lightroom-classic": [appdata / "Adobe" / "Lightroom"],
            "bridge": [appdata / "Adobe" / "Bridge*"],
            "common": [appdata / "Adobe" / "Color", appdata / "Adobe" / "CEP"],
        }
    if is_macos():
        app_sup = h / "Library" / "Application Support" / "Adobe"
        prefs = h / "Library" / "Preferences" / "Adobe"
        docs = h / "Documents" / "Adobe"
        return {
            "photoshop": [app_sup / "Adobe Photoshop*"],
            "illustrator": [app_sup / "Adobe Illustrator*"],
            "indesign": [app_sup / "InDesign" / "Version*"],
            "premiere": [docs / "Premiere Pro"],
            "after-effects": [docs / "After Effects*"],
            "lightroom-classic": [app_sup / "Lightroom"],
            "bridge": [app_sup / "Bridge*", prefs / "Bridge*"],
            "common": [app_sup / "Color", app_sup / "CEP"],
        }
    return {}


def font_cache_dirs() -> List[Path]:
    """Where CoreSync drops activated Adobe Fonts (used to recover the list)."""
    h = home()
    if is_windows():
        appdata = Path(os.environ.get("APPDATA", h / "AppData" / "Roaming"))
        return [appdata / "Adobe" / "CoreSync" / "plugins" / "livetype"]
    if is_macos():
        return [h / "Library" / "Application Support" / "Adobe" / "CoreSync" / "plugins" / "livetype"]
    return []


# --------------------------------------------------------------------------
# Runtime settings
# --------------------------------------------------------------------------
@dataclass
class Settings:
    output: Path = field(default_factory=default_output)
    workers: int = 6
    timeout: float = 60.0
    retries: int = 5
    chunk_size: int = 1024 * 1024
    rate_limit: float = 0.0          # seconds to sleep between requests per worker
    min_free_gb: float = 2.0
    dry_run: bool = False
    verify: bool = True              # checksum files after download
    overwrite: bool = False
    #: Skip files already downloaded. On by default: an interrupted 740 MB
    #: run should cost nothing to repeat.
    resume: bool = True
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    max_size_mb: Optional[float] = None
    sources: List[str] = field(default_factory=list)
    language: str = "tr"
    verbose: bool = False

    def to_json(self) -> str:
        data: Dict[str, Any] = asdict(self)
        data["output"] = str(self.output)
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "output" in data:
            data["output"] = Path(data["output"])
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def platform_summary() -> str:
    return f"{platform.system()} {platform.release()} / Python {platform.python_version()}"
