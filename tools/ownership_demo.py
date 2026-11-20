#!/usr/bin/env python3
"""Entry point kept at tools/ownership_demo.py for discoverability.

Equivalent to ``python3 -m telber.ownership_demo``; see
:mod:`telber.ownership_demo`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telber.ownership_demo import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
