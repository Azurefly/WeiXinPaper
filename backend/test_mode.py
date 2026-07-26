from __future__ import annotations

import os
from pathlib import Path

_MARKER = Path(__file__).resolve().parent.parent / ".test-adapters-enabled"


def enabled(name: str) -> bool:
    """Test adapters require both explicit environment flags and a source-only marker.

    The marker is deliberately excluded from runtime packages, so production bundles
    cannot activate deterministic fake AI/WeChat success by setting environment variables.
    """
    return (
        _MARKER.is_file()
        and os.environ.get("STUDIO_ENABLE_TEST_ADAPTERS", "") == "1"
        and os.environ.get(name, "") == "1"
    )
