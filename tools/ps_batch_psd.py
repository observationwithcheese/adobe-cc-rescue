"""Thin wrapper kept so the documented path still works from a checkout.

The converter lives in adobe_rescue/psd_convert.py so it ships inside the
packaged executable too (`adobe-rescue psd ...`).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adobe_rescue.psd_convert import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
