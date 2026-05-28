import { useRef, useState } from "react";
import PdfSemanticViewer from "./components/PdfSemanticViewer";
import { uploadPdf, type ExtractResponse, type UploadResponse } from "./api";

function FileUploadButton({
  disabled,
  onFile,
  label = "Upload PDF",
}: {
  disabled?: boolean;
  onFile: (file: File) => void;
  label?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <label className="upload-btn">
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        disabled={disabled}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          e.target.value = "";
        }}
      />
      {label}
    </label>
  );
}

export default function App() {
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [page, setPage] = useState(1);
  const [extractResult, setExtractResult] = useState<ExtractResponse | null>(
    null,
  );
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onFile = async (file: File) => {
    setUploading(true);
    setError(null);
    setExtractResult(null);
    try {
      const res = await uploadPdf(file);
      setUpload(res);
      setPage(1);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Interactive Semantic Extraction</h1>
          <p className="subtitle">
            Upload a drawing, drag a box over a dimension cluster, get structured
            JSON from the reconstruction engine.
          </p>
        </div>
        <div className="upload-area">
          <FileUploadButton
            disabled={uploading}
            onFile={(f) => void onFile(f)}
            label={upload ? "Replace PDF" : "Upload PDF"}
          />
          {uploading && <span className="upload-status">Uploading…</span>}
        </div>
      </header>

      {error && <p className="error-banner">{error}</p>}

      {upload && (
        <div className="meta-bar">
          <span className="filename">{upload.filename}</span>
          <span>·</span>
          <span>{upload.page_count} page{upload.page_count === 1 ? "" : "s"}</span>
          <span>·</span>
          <label>
            Page{" "}
            <input
              type="number"
              min={1}
              max={upload.page_count}
              value={page}
              onChange={(e) =>
                setPage(Math.max(1, parseInt(e.target.value, 10) || 1))
              }
            />
          </label>
        </div>
      )}

      <main className="workspace">
        <section className="viewer-column">
          {upload ? (
            <PdfSemanticViewer
              key={`${upload.document_id}-${page}`}
              documentId={upload.document_id}
              pageNumber={page}
              onExtract={setExtractResult}
            />
          ) : (
            <div className="placeholder">
              <div className="placeholder-icon" aria-hidden>
                📐
              </div>
              <h3>No drawing loaded</h3>
              <p>Upload an engineering PDF to render the sheet and extract dimensions from a selected region.</p>
              <FileUploadButton
                disabled={uploading}
                onFile={(f) => void onFile(f)}
              />
            </div>
          )}
        </section>

        <aside className="json-panel">
          <div className="json-panel-header">
            <h2>Extracted entities</h2>
          </div>
          <div className="json-panel-body">
            {extractResult ? (
              <>
                <p className="entity-count">
                  {extractResult.entity_count} dimension
                  {extractResult.entity_count === 1 ? "" : "s"} in selection
                </p>
                <pre>{JSON.stringify(extractResult.entities, null, 2)}</pre>
              </>
            ) : (
              <p className="muted">
                Drag a rectangle on the drawing over dimensions such as{" "}
                <strong>50.7 ±0.2</strong>, <strong>3 THK</strong>, or{" "}
                <strong>R10</strong>. Results appear here as JSON.
              </p>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}
