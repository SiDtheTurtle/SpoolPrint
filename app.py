"""NiimBatch: batch-print 3dfilamentprofiles.com spool labels to a Niimbot B1."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

API_KEY = os.environ.get("FILAMENTPROFILES_API_KEY", "")
DATA_DIR = Path(os.environ.get("NB_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="NiimBatch", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz():
    return {"ok": True, "api_key_set": bool(API_KEY)}


# Mounted last so /healthz and future /api routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
