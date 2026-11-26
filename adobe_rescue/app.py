"""Entry point for the double-clickable builds.

The packaged executable has to be two things at once: the window a designer
double-clicks, and the command line the app re-invokes on itself to show the
Adobe login webview in a child process (the webview needs the main thread,
which tkinter already owns).

So: arguments → CLI, no arguments → GUI. tkinter is imported only on the GUI
path, which keeps this module testable where tkinter is absent.
"""

from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        from adobe_rescue.cli import main as cli_main
        return cli_main(args)
    from adobe_rescue.gui import main as gui_main
    return gui_main()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
