# Interactive Semantic Extraction Viewer

Human-guided region extraction for engineering drawings.

See the main project overview: [../README.md](../README.md)

## Prerequisites

- Python venv at `../.venv` with PyMuPDF and API deps
- Node.js 18+

## 1. Start API (port 8000)

```bat
cd ..
.venv\Scripts\activate.bat
pip install -r requirements-api.txt
python -m uvicorn api_server:app --reload --port 8000
```

## 2. Start UI (port 5173)

```bat
cd viewer
npm install
npm run dev
```

Open http://localhost:5173 — upload a PDF, drag a rectangle over dimensions, JSON appears in the right panel.

## Flow

1. Upload PDF → stored under `uploads/`
2. PDF.js renders the page (same appearance as the drawing)
3. Mouse position → `pdfCoordinates.ts` (Fitz ↔ PDF.js viewport) → backend API
4. Hover calls `POST /api/documents/{id}/snap`; click or drag calls `/extract`
5. Overlays use `convertToViewportRectangle()`; highlights stay aligned on zoom
6. JSON appears in the right panel; enable **Debug bboxes** to verify red alignment
