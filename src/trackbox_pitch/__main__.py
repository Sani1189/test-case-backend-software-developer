"""Support ``python -m trackbox_pitch`` as well as the ``pitch-pipeline`` script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
