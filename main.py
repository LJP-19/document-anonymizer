#!/usr/bin/env python3
"""Top-level launcher so the packaged binary has a single obvious entry point."""

import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
