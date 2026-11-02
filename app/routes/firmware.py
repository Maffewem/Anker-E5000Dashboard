"""Web routes: the ESP32 round display's firmware, so the board can update itself from the dashboard.

Read-only and behind the same WIDGET_TOKEN as /api/widget. The files come from the firmware/ folder (built in CI
and committed to firmware/dist/ in the repo, copied into the image); the board compares the MD5 and downloads the
.bin when it differs.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .widget import require_token

router = APIRouter()
NAME = "esp32-round"


def _bin() -> Path:
    folder = os.environ.get("FIRMWARE_DIR") or str(Path(__file__).resolve().parents[2] / "firmware")
    return Path(folder) / f"{NAME}.bin"


def _existing() -> Path:
    path = _bin()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No firmware built yet")
    return path


@router.get(f"/api/firmware/{NAME}")
def firmware_info(request: Request) -> dict:
    """MD5 and size of the current build, for the board to compare with what it runs."""
    require_token(request)
    path = _existing()
    return {"md5": hashlib.md5(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


@router.get(f"/api/firmware/{NAME}.bin")
def firmware_bin(request: Request) -> FileResponse:
    require_token(request)
    return FileResponse(_existing(), media_type="application/octet-stream")
