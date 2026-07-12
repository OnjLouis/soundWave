# -*- coding: utf-8 -*-
"""NVDA global plugin entry point for soundWave."""

from __future__ import annotations

import os
import sys

_ADDON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADDON_ROOT not in sys.path:
	sys.path.insert(0, _ADDON_ROOT)

for _name in [name for name in list(sys.modules) if name == "soundWave_lib" or name.startswith("soundWave_lib.")]:
	del sys.modules[_name]

from soundWave_lib.main import GlobalPlugin
