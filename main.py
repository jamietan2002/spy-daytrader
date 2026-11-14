#!/usr/bin/env python3
"""Top-level CLI entrypoint for the SPY Intraday Paper Day-Trading Bot."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running `python main.py ...` directly without editable install
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spy_daytrader.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

