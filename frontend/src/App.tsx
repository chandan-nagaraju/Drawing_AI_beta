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
  const [inspectionRows, setInspectionRows] = useState<
    Array<{
      balloon_no: number;
      entity_id: string;
      display_text: string;
      page: number;
      bbox: number[];
      entity_type: string;
      rejected: boolean;
      raw: Record<string, unknown>;
    }>
  >([]);
  const [activeBalloonId, setActiveBalloonId] = useState<string | null>(null);
  const [lastSelectionStats, setLastSelectionStats] = useState<{
    hits: number;
    added: number;
    reused: number;
  } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRejected, setLastRejected] = useState<string | null>(null);
  const [showRejected, setShowRejected] = useState(false);
  const [balloonMode, setBalloonMode] = useState(false);
  const [printNotice, setPrintNotice] = useState<string | null>(null);

  const roundCoord = (value: number) => Math.round(value * 2) / 2;

  const buildEntityId = (
    pageNo: number,
    displayText: string,
    bbox: number[],
    entityType: string,
  ) => {
    const b = bbox.map((n) => roundCoord(n)).join("_");
    return `${pageNo}|${entityType}|${displayText}|${b}`;
  };

  const normalizeDisplay = (value: unknown): string =>
    String(value ?? "")
      .trim()
      .replace(/\s+/g, "")
      .replace(/×/g, "X")
      .replace(/[�]/g, "±");

  const formatParameter = (entityType: string) =>
    entityType
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());

  const onExtract = (result: ExtractResponse) => {
    setInspectionRows((prev) => {
      const next = [...prev];
      const idToIndex = new Map(next.map((row, idx) => [row.entity_id, idx]));
      let newestId: string | null = null;

      let addedCount = 0;
      let reusedCount = 0;
      for (const ent of result.entities) {
        const raw = ent as Record<string, unknown>;
        const display = normalizeDisplay(raw.display_text);
        const pageNo = Number(raw.page ?? result.page);
        const bbox = Array.isArray(raw.text_bbox)
          ? (raw.text_bbox as number[]).slice(0, 4)
          : [0, 0, 0, 0];
        if (!display || bbox.length !== 4) continue;
        const entityType = String(raw.entity_type ?? "dimension");
        const entityId = buildEntityId(pageNo, display, bbox, entityType);

        if (idToIndex.has(entityId)) {
          newestId = entityId;
          reusedCount += 1;
          continue;
        }

        const row = {
          balloon_no: next.length + 1,
          entity_id: entityId,
          display_text: display,
          page: pageNo,
          bbox,
          entity_type: entityType,
          rejected: false,
          raw,
        };
        next.push(row);
        idToIndex.set(entityId, next.length - 1);
        newestId = entityId;
        addedCount += 1;
      }

      setLastSelectionStats({
        hits: result.entity_count,
        added: addedCount,
        reused: reusedCount,
      });
      if (newestId) setActiveBalloonId(newestId);
      return next;
    });
  };

  const rejectEntity = (entityId: string) => {
    setInspectionRows((prev) =>
      prev.map((row) =>
        row.entity_id === entityId ? { ...row, rejected: true } : row,
      ),
    );
    setLastRejected(entityId);
    setActiveBalloonId(entityId);
  };

  const undoReject = () => {
    if (!lastRejected) return;
    setInspectionRows((prev) =>
      prev.map((row) =>
        row.entity_id === lastRejected ? { ...row, rejected: false } : row,
      ),
    );
    setActiveBalloonId(lastRejected);
    setLastRejected(null);
  };

  const restoreEntity = (entityId: string) => {
    setInspectionRows((prev) =>
      prev.map((row) =>
        row.entity_id === entityId ? { ...row, rejected: false } : row,
      ),
    );
    setActiveBalloonId(entityId);
  };

  const centerOf = (bbox: number[]) => ({
    x: (bbox[0] + bbox[2]) / 2,
    y: (bbox[1] + bbox[3]) / 2,
  });

  const reorderClockwise = () => {
    setInspectionRows((prev) => {
      if (prev.length <= 1) return prev;

      const active = prev.filter((r) => !r.rejected && r.bbox.length === 4);
      if (active.length <= 1) return prev;

      const centroid = active.reduce(
        (acc, row) => {
          const c = centerOf(row.bbox);
          return { x: acc.x + c.x, y: acc.y + c.y };
        },
        { x: 0, y: 0 },
      );
      centroid.x /= active.length;
      centroid.y /= active.length;

      const start = active.reduce((best, cur) =>
        cur.balloon_no < best.balloon_no ? cur : best,
      );
      const startCenter = centerOf(start.bbox);
      const startAngle = Math.atan2(startCenter.y - centroid.y, startCenter.x - centroid.x);

      const ordered = [...active]
        .map((row) => {
          const c = centerOf(row.bbox);
          const angle = Math.atan2(c.y - centroid.y, c.x - centroid.x);
          const clockwiseDelta = (startAngle - angle + Math.PI * 2) % (Math.PI * 2);
          const radius = Math.hypot(c.x - centroid.x, c.y - centroid.y);
          return { row, clockwiseDelta, radius };
        })
        .sort((a, b) => {
          if (Math.abs(a.clockwiseDelta - b.clockwiseDelta) > 1e-6) {
            return a.clockwiseDelta - b.clockwiseDelta;
          }
          return a.radius - b.radius;
        })
        .map((x) => x.row);

      const nextBalloonById = new Map<string, number>();
      ordered.forEach((row, idx) => nextBalloonById.set(row.entity_id, idx + 1));

      const rejected = prev
        .filter((r) => r.rejected)
        .sort((a, b) => a.balloon_no - b.balloon_no);

      rejected.forEach((row, idx) => {
        nextBalloonById.set(row.entity_id, ordered.length + idx + 1);
      });

      return prev.map((row) => ({
        ...row,
        balloon_no: nextBalloonById.get(row.entity_id) ?? row.balloon_no,
      }));
    });
  };

  const onFile = async (file: File) => {
    setUploading(true);
    setError(null);
    setInspectionRows([]);
    setActiveBalloonId(null);
    setLastSelectionStats(null);
    setLastRejected(null);
    setShowRejected(false);
    setBalloonMode(false);
    setPrintNotice(null);
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

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  const runBalloonPrintFlow = async (mode: "save" | "print") => {
    if (!upload) return;
    setError(null);
    setPrintNotice(
      mode === "save"
        ? "Opening print dialog. Choose destination: Save as PDF."
        : "Opening print dialog for ballooned printout.",
    );

    if (!balloonMode) {
      setBalloonMode(true);
      await sleep(120);
    }
    const prevTitle = document.title;
    document.title = "";
    window.print();
    window.setTimeout(() => {
      document.title = prevTitle;
    }, 250);
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
          <span>·</span>
          <div className="meta-actions">
            <button
              type="button"
              className="meta-btn"
              onClick={() => void runBalloonPrintFlow("save")}
            >
              Save Ballooned PDF
            </button>
            <button
              type="button"
              className="meta-btn"
              onClick={() => void runBalloonPrintFlow("print")}
            >
              Print Ballooned
            </button>
          </div>
        </div>
      )}
      {printNotice && <p className="print-notice">{printNotice}</p>}

      <main className="workspace">
        <section className="viewer-column">
          {upload ? (
            <PdfSemanticViewer
              key={`${upload.document_id}-${page}`}
              documentId={upload.document_id}
              pageNumber={page}
              onExtract={onExtract}
              balloonMode={balloonMode}
              onToggleBalloonMode={() => setBalloonMode((v) => !v)}
              balloonItems={inspectionRows
                .filter((row) => !row.rejected)
                .map((row) => ({
                  id: row.entity_id,
                  balloonNo: row.balloon_no,
                  page: row.page,
                  bbox: row.bbox,
                }))}
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
            <h2>Inspection Balloons</h2>
          </div>
          <div className="json-panel-body">
            {inspectionRows.length > 0 ? (
              <>
                {lastRejected && (
                  <div className="undo-bar">
                    <span>Entity rejected.</span>
                    <button type="button" className="undo-btn" onClick={undoReject}>
                      Undo
                    </button>
                  </div>
                )}
                <p className="entity-count">
                  {(() => {
                    const activeCount = inspectionRows.filter((r) => !r.rejected).length;
                    const rejectedCount = inspectionRows.length - activeCount;
                    return (
                      <>
                        Session: {activeCount} active balloon
                        {activeCount === 1 ? "" : "s"}
                        {rejectedCount > 0 && ` · ${rejectedCount} rejected`}
                      </>
                    );
                  })()}
                  {lastSelectionStats && (
                    <>
                      {" "}· Last selection: {lastSelectionStats.hits} hit
                      {lastSelectionStats.hits === 1 ? "" : "s"}
                      {` (${lastSelectionStats.reused} reused, ${lastSelectionStats.added} added)`}
                    </>
                  )}
                </p>
                <div className="table-controls">
                  <label className="show-rejected-toggle">
                    <input
                      type="checkbox"
                      checked={showRejected}
                      onChange={(e) => setShowRejected(e.target.checked)}
                    />
                    Show rejected
                  </label>
                  <button
                    type="button"
                    className="table-order-btn"
                    onClick={reorderClockwise}
                    title="Renumber balloons clockwise from current #1"
                  >
                    Clockwise Order
                  </button>
                </div>
                <table className="balloon-table">
                  <thead>
                    <tr>
                      <th>Balloon</th>
                      <th>Parameter</th>
                      <th>Value</th>
                      {showRejected && <th>Status</th>}
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inspectionRows
                      .filter((row) => (showRejected ? true : !row.rejected))
                      .map((row) => (
                      <tr
                        key={row.entity_id}
                        className={[
                          row.entity_id === activeBalloonId ? "active" : "",
                          row.rejected ? "rejected-row" : "",
                        ]
                          .join(" ")
                          .trim()}
                      >
                        <td>{row.balloon_no}</td>
                        <td>{formatParameter(row.entity_type)}</td>
                        <td>{row.display_text}</td>
                        {showRejected && <td>{row.rejected ? "Rejected" : "Active"}</td>}
                        <td>
                          {row.rejected ? (
                            <button
                              type="button"
                              className="restore-btn"
                              onClick={() => restoreEntity(row.entity_id)}
                              aria-label={`Restore balloon ${row.balloon_no}`}
                              title="Restore"
                            >
                              ↺
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="reject-btn"
                              onClick={() => rejectEntity(row.entity_id)}
                              aria-label={`Reject balloon ${row.balloon_no}`}
                              title="Reject"
                            >
                              ×
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : (
              <p className="muted">
                Drag/select dimensions on one or multiple views. Balloon list is
                cumulative, deduplicated, and keeps stable numbering across overlapping selections.
              </p>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}
