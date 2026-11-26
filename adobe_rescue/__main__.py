"""Allow `python -m adobe_rescue`."""

from adobe_rescue.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
