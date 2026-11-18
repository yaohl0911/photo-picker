# -*- coding: utf-8 -*-
"""photo-picker backend package.

Importing ``backend`` puts the ``backend/`` directory itself on
``sys.path`` so the intra-package imports written as ``from app import
...`` (api/main/tests all use this style) resolve when the package is
entered from the repository root, e.g.::

    .venv/bin/python -m backend.app.main      # run.sh entry point
    .venv/bin/python -c "from backend.app.api import get_settings"

Without this bootstrap the repo root is the only sys.path entry and the
top-level name ``app`` would not resolve. Idempotent: repeated imports
(backend/__init__.py runs before backend.app.*) never duplicate entries.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
