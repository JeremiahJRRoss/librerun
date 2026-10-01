"""The SDK's tests run against ``src`` without an install."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Capture is exercised explicitly (tests/test_capture.py); a test process
# that captured its own stdout would swallow pytest's output.
os.environ.setdefault("LIBRERUN_AGENT_CAPTURE_STDIO", "0")
