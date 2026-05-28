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
pip install -r backend/requirements-api.txt
cd backend
python -m uvicorn api_server:app --reload --port 8000
```

## 2. Start UI (port 5173)

```bat
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — upload a PDF, extract dimensions, then review in the inspection balloon table.

## Flow

1. Upload PDF → stored under `backend/uploads/`
2. PDF.js renders the page (same appearance as the drawing)
3. Mouse position → `pdfCoordinates.ts` (Fitz ↔ PDF.js viewport) → backend API
4. Hover calls `POST /api/documents/{id}/snap`; click or drag calls `/extract`
5. Overlays use `convertToViewportRectangle()`; highlights stay aligned on zoom
6. Right panel is a cumulative balloon table (`Balloon`, `Parameter`, `Value`, `Action`)
7. Rows support soft reject, undo, show rejected, and restore
8. **Clockwise Order** renumbers balloons around the drawing spread
9. **Balloon** mode overlays draggable balloon numbers with anchor lines
10. **Save Ballooned PDF** / **Print Ballooned** print only the drawing + balloons
11. Enable **Debug bboxes** to verify red alignment
