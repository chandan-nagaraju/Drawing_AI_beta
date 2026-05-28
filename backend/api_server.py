"""
FastAPI backend for Interactive Semantic Extraction Viewer.

Run:
  .venv\\Scripts\\python.exe -m uvicorn api_server:app --reload --port 8000
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List

import fitz
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from region_semantic_extraction import extract_region_semantics, snap_semantic_entity

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="Drawing AI — Semantic Extraction API",
    description="Region-scoped engineering dimension reconstruction",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    page: int = Field(1, ge=1, description="1-based page number")
    bbox: List[float] = Field(..., min_length=4, max_length=4, description="PDF points [x0,y0,x1,y1]")
    glyph_mode: str = "intersects"


class SnapRequest(BaseModel):
    page: int = Field(1, ge=1, description="1-based page number")
    x: float
    y: float
    radius: float = Field(24.0, gt=2.0, le=80.0)
    glyph_mode: str = "intersects"


def _doc_path(doc_id: str) -> Path:
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    doc_id = str(uuid.uuid4())
    dest = UPLOAD_DIR / f"{doc_id}.pdf"

    content = await file.read()
    if len(content) < 64:
        raise HTTPException(status_code=400, detail="PDF file is empty or invalid")

    dest.write_bytes(content)

    doc = fitz.open(dest)
    try:
        page_count = doc.page_count
        first = doc[0]
        page_width = first.rect.width
        page_height = first.rect.height
    finally:
        doc.close()

    return {
        "document_id": doc_id,
        "filename": file.filename,
        "page_count": page_count,
        "page_width": page_width,
        "page_height": page_height,
    }


@app.get("/api/documents/{doc_id}/file")
def get_document_file(doc_id: str):
    """Return full PDF bytes (no range requests — avoids proxy/PDF.js 204 issues)."""
    path = _doc_path(doc_id)
    data = path.read_bytes()
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Length": str(len(data)),
            "Content-Disposition": f'inline; filename="{doc_id}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/documents/{doc_id}/pages/{page_num}")
def get_page_info(doc_id: str, page_num: int):
    path = _doc_path(doc_id)
    doc = fitz.open(path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            raise HTTPException(status_code=400, detail=f"Page must be 1..{doc.page_count}")
        page = doc[page_num - 1]
        return {
            "document_id": doc_id,
            "page": page_num,
            "width": page.rect.width,
            "height": page.rect.height,
            "page_count": doc.page_count,
        }
    finally:
        doc.close()


@app.post("/api/documents/{doc_id}/extract")
def extract_region(doc_id: str, body: ExtractRequest):
    path = _doc_path(doc_id)
    try:
        result = extract_region_semantics(
            str(path),
            body.page,
            body.bbox,
            glyph_mode=body.glyph_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["document_id"] = doc_id
    return result


@app.post("/api/documents/{doc_id}/snap")
def snap_entity(doc_id: str, body: SnapRequest):
    path = _doc_path(doc_id)
    try:
        result = snap_semantic_entity(
            str(path),
            body.page,
            body.x,
            body.y,
            radius=body.radius,
            glyph_mode=body.glyph_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["document_id"] = doc_id
    return result
