import { useCallback, useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import type { PageViewport } from "pdfjs-dist/types/src/display/display_utils";
import {
  extractRegion,
  fetchPdfBytes,
  snapEntity,
  type ExtractResponse,
} from "../api";
import {
  pdfBboxToScreen,
  screenRectToFitzBbox,
  screenToFitzPoint,
  type ScreenRect,
} from "../pdfCoordinates";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

type Props = {
  documentId: string;
  pageNumber: number;
  onExtract?: (result: ExtractResponse) => void;
  balloonMode?: boolean;
  onToggleBalloonMode?: () => void;
  balloonItems?: Array<{
    id: string;
    balloonNo: number;
    page: number;
    bbox: number[];
  }>;
};

type HoveredEntity = {
  data: Record<string, unknown>;
  pdfBbox: number[];
};

export default function PdfSemanticViewer({
  documentId,
  pageNumber,
  onExtract,
  balloonMode = false,
  onToggleBalloonMode,
  balloonItems = [],
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [viewport, setViewport] = useState<PageViewport | null>(null);
  const [scale, setScale] = useState(1.2);
  const [dragging, setDragging] = useState(false);
  const [selection, setSelection] = useState<ScreenRect | null>(null);
  const [dragStart, setDragStart] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [highlightPdfBboxes, setHighlightPdfBboxes] = useState<number[][]>([]);
  const [debugBboxes, setDebugBboxes] = useState(false);
  const [hoveredEntity, setHoveredEntity] = useState<HoveredEntity | null>(null);
  const [hoverPoint, setHoverPoint] = useState<{ x: number; y: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastBbox, setLastBbox] = useState<number[] | null>(null);
  const [zoomInput, setZoomInput] = useState("120");
  const hoverTimerRef = useRef<number | null>(null);
  const hoverReqIdRef = useRef(0);
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);
  const renderGenRef = useRef(0);
  const [balloonPositions, setBalloonPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});
  const [dragBalloon, setDragBalloon] = useState<{
    id: string;
    dx: number;
    dy: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPdf(null);
    setError(null);
    (async () => {
      const data = await fetchPdfBytes(documentId);
      const doc = await pdfjs.getDocument({
        data,
        disableRange: true,
        disableStream: true,
      }).promise;
      if (!cancelled) setPdf(doc);
    })().catch((e: unknown) => {
      if (!cancelled) {
        setError(e instanceof Error ? e.message : "Failed to load PDF");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  const cancelActiveRender = useCallback(() => {
    const task = renderTaskRef.current;
    if (!task) return;
    try {
      task.cancel();
    } catch {
      // ignore — task may already be finished
    }
    renderTaskRef.current = null;
  }, []);

  useEffect(() => {
    if (!dragBalloon) return;
    const onMove = (ev: MouseEvent) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      const x = (ev.clientX - rect.left) * scaleX - dragBalloon.dx;
      const y = (ev.clientY - rect.top) * scaleY - dragBalloon.dy;
      setBalloonPositions((prev) => ({
        ...prev,
        [dragBalloon.id]: { x, y },
      }));
    };
    const onUp = () => setDragBalloon(null);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragBalloon]);

  useEffect(() => {
    setZoomInput(String(Math.round(scale * 100)));
  }, [scale]);

  const clampScale = (value: number) => Math.max(0.2, Math.min(5, value));

  const zoomIn = useCallback(() => {
    setScale((prev) => clampScale(prev + 0.1));
  }, []);

  const zoomOut = useCallback(() => {
    setScale((prev) => clampScale(prev - 0.1));
  }, []);

  const zoom100 = useCallback(() => {
    setScale(1);
  }, []);

  const fitToScreen = useCallback(async () => {
    if (!pdf || !wrapRef.current) return;
    const page = await pdf.getPage(pageNumber);
    const base = page.getViewport({ scale: 1 });
    const pad = 8;
    const fitW = (wrapRef.current.clientWidth - pad * 2) / base.width;
    const fitH = (window.innerHeight * 0.68) / base.height;
    const next = clampScale(Math.min(fitW, fitH));
    setScale(next);
  }, [pdf, pageNumber]);

  const applyZoomInput = useCallback(() => {
    const parsed = Number.parseFloat(zoomInput);
    if (Number.isNaN(parsed)) {
      setZoomInput(String(Math.round(scale * 100)));
      return;
    }
    setScale(clampScale(parsed / 100));
  }, [zoomInput, scale]);

  const isRenderCancelled = (e: unknown) => {
    if (!(e instanceof Error)) return false;
    const msg = e.message.toLowerCase();
    return msg.includes("cancel") || msg.includes("same canvas");
  };

  const renderPage = useCallback(async () => {
    if (!pdf || !canvasRef.current) return;

    cancelActiveRender();
    const gen = ++renderGenRef.current;

    const page: PDFPageProxy = await pdf.getPage(pageNumber);
    if (gen !== renderGenRef.current) return;

    const vp = page.getViewport({ scale });
    setViewport(vp);

    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    canvas.width = vp.width;
    canvas.height = vp.height;

    const task = page.render({ canvasContext: ctx, viewport: vp });
    renderTaskRef.current = task;

    try {
      await task.promise;
      if (gen === renderGenRef.current) {
        setError((prev) =>
          prev?.toLowerCase().includes("same canvas") ? null : prev,
        );
      }
    } catch (e: unknown) {
      if (isRenderCancelled(e) || gen !== renderGenRef.current) return;
      throw e;
    } finally {
      if (renderTaskRef.current === task) {
        renderTaskRef.current = null;
      }
    }
  }, [pdf, pageNumber, scale, cancelActiveRender]);

  useEffect(() => {
    let active = true;
    renderPage().catch((e: unknown) => {
      if (!active || isRenderCancelled(e)) return;
      setError(e instanceof Error ? e.message : "Render failed");
    });
    return () => {
      active = false;
      renderGenRef.current += 1;
      cancelActiveRender();
    };
  }, [renderPage, cancelActiveRender]);

  useEffect(() => {
    return () => {
      if (hoverTimerRef.current !== null) {
        window.clearTimeout(hoverTimerRef.current);
      }
    };
  }, []);

  const localPoint = (e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: (e.clientX - rect.left) * scaleX,
      y: (e.clientY - rect.top) * scaleY,
    };
  };

  const runExtractionFromPdfBbox = async (bbox: number[]) => {
    if (!viewport) return;
    setLastBbox(bbox);
    setLoading(true);
    setError(null);
    try {
      const result = await extractRegion(documentId, pageNumber, bbox);
      onExtract?.(result);
      const bboxes = result.entities
        .map((ent) => ent.text_bbox as number[] | undefined)
        .filter((b): b is number[] => Array.isArray(b) && b.length === 4);
      setHighlightPdfBboxes(bboxes);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Extraction failed");
      setHighlightPdfBboxes([]);
    } finally {
      setLoading(false);
    }
  };

  const runExtraction = async (screenRect: ScreenRect) => {
    if (!viewport || screenRect.w < 4 || screenRect.h < 4) return;
    const bbox = screenRectToFitzBbox(screenRect, viewport);
    await runExtractionFromPdfBbox(bbox);
  };

  const scheduleHoverSnap = (screenX: number, screenY: number) => {
    if (!viewport || dragging) return;
    setHoverPoint({ x: screenX, y: screenY });

    if (hoverTimerRef.current !== null) {
      window.clearTimeout(hoverTimerRef.current);
    }
    hoverTimerRef.current = window.setTimeout(async () => {
      const [px, py] = screenToFitzPoint(screenX, screenY, viewport);
      const reqId = ++hoverReqIdRef.current;
      try {
        const snap = await snapEntity(
          documentId,
          pageNumber,
          px,
          py,
          Math.max(14, 24 / scale),
        );
        if (reqId !== hoverReqIdRef.current) return;
        const selected = snap.selected;
        const bbox = (selected?.text_bbox as number[] | undefined) ?? null;
        if (!selected || !bbox || bbox.length !== 4) {
          setHoveredEntity(null);
          return;
        }
        setHoveredEntity({ data: selected, pdfBbox: bbox });
      } catch {
        if (reqId !== hoverReqIdRef.current) return;
        setHoveredEntity(null);
      }
    }, 60);
  };

  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    const p = localPoint(e);
    setDragging(true);
    setDragStart(p);
    setSelection({ x: p.x, y: p.y, w: 0, h: 0 });
    setHoveredEntity(null);
  };

  const onMouseMove = (e: React.MouseEvent) => {
    const p = localPoint(e);
    if (!dragging || !dragStart) {
      scheduleHoverSnap(p.x, p.y);
      return;
    }
    setSelection({
      x: Math.min(dragStart.x, p.x),
      y: Math.min(dragStart.y, p.y),
      w: Math.abs(p.x - dragStart.x),
      h: Math.abs(p.y - dragStart.y),
    });
  };

  const onMouseUp = (e: React.MouseEvent) => {
    if (!dragging) return;
    setDragging(false);
    const p = localPoint(e);
    const rect: ScreenRect = {
      x: Math.min(dragStart!.x, p.x),
      y: Math.min(dragStart!.y, p.y),
      w: Math.abs(p.x - dragStart!.x),
      h: Math.abs(p.y - dragStart!.y),
    };
    const isClick = rect.w < 4 && rect.h < 4;
    if (isClick && hoveredEntity) {
      setSelection(null);
      const b = hoveredEntity.pdfBbox;
      const pad = 3;
      void runExtractionFromPdfBbox([b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]);
      return;
    }
    setSelection(rect);
    void runExtraction(rect);
  };

  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    if (e.deltaY < 0) {
      zoomIn();
    } else {
      zoomOut();
    }
  };

  return (
    <div className="viewer-panel">
      <div className="viewer-toolbar">
        <div className="zoom-controls" role="group" aria-label="Zoom controls">
          <button type="button" className="tool-btn" onClick={() => void fitToScreen()}>
            Fit
          </button>
          <button type="button" className="tool-btn icon-btn" onClick={zoomOut}>
            -
          </button>
          <input
            className="zoom-input"
            value={zoomInput}
            onChange={(e) => setZoomInput(e.target.value.replace(/[^\d.]/g, ""))}
            onBlur={applyZoomInput}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                applyZoomInput();
                (e.target as HTMLInputElement).blur();
              }
            }}
            aria-label="Zoom percent"
          />
          <span className="zoom-suffix">%</span>
          <button type="button" className="tool-btn icon-btn" onClick={zoomIn}>
            +
          </button>
          <button type="button" className="tool-btn" onClick={zoom100}>
            100%
          </button>
        </div>
        <button
          type="button"
          className={`tool-btn ${balloonMode ? "tool-btn-active" : ""}`}
          onClick={onToggleBalloonMode}
          title="Toggle balloon overlay"
        >
          Balloon
        </button>
        <label className="debug-toggle">
          <input
            type="checkbox"
            checked={debugBboxes}
            onChange={(e) => setDebugBboxes(e.target.checked)}
          />
          Debug bboxes
        </label>
        {loading && <span className="status loading">Extracting…</span>}
        {lastBbox && !loading && (
          <span className="status region">
            Region [{lastBbox.map((n) => n.toFixed(0)).join(", ")}]
          </span>
        )}
      </div>

      {error && <p className="error-banner">{error}</p>}

      <div
        className="canvas-wrap"
        ref={wrapRef}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onWheel={onWheel}
        onMouseLeave={() => {
          if (dragging) setDragging(false);
          setHoverPoint(null);
          setHoveredEntity(null);
        }}
      >
        <canvas ref={canvasRef} className="pdf-canvas" />
        <div
          className="overlay"
          style={{
            width: viewport?.width ?? 0,
            height: viewport?.height ?? 0,
          }}
        >
          {selection && selection.w > 0 && selection.h > 0 && (
            <div
              className="selection-rect"
              style={{
                left: selection.x,
                top: selection.y,
                width: selection.w,
                height: selection.h,
              }}
            />
          )}
          {viewport &&
            highlightPdfBboxes.map((bbox, i) => {
              const r = pdfBboxToScreen(bbox, viewport);
              return (
                <div
                  key={`hl-${i}`}
                  className={debugBboxes ? "debug-bbox" : "entity-highlight"}
                  style={{ left: r.x, top: r.y, width: r.w, height: r.h }}
                />
              );
            })}
          {hoveredEntity && viewport && (() => {
            const snapRect = pdfBboxToScreen(hoveredEntity.pdfBbox, viewport);
            return (
            <>
              <div
                className="snap-highlight"
                style={{
                  left: snapRect.x,
                  top: snapRect.y,
                  width: snapRect.w,
                  height: snapRect.h,
                }}
              />
              <div
                className="snap-tooltip"
                style={{
                  left: hoverPoint ? hoverPoint.x + 10 : snapRect.x,
                  top: hoverPoint ? hoverPoint.y - 30 : snapRect.y - 8,
                }}
              >
                <strong>{String(hoveredEntity.data.display_text ?? "")}</strong>
                <span>{String(hoveredEntity.data.entity_type ?? "")}</span>
              </div>
            </>
            );
          })()}
          {balloonMode &&
            viewport &&
            balloonItems
              .filter((item) => item.page === pageNumber && item.bbox.length === 4)
              .map((item) => {
                const target = pdfBboxToScreen(item.bbox, viewport);
                const targetCx = target.x + target.w / 2;
                const targetCy = target.y + target.h / 2;
                const saved = balloonPositions[item.id];
                const bx = saved ? saved.x : targetCx + 34;
                const by = saved ? saved.y : targetCy - 34;
                const nodeCx = bx + 10;
                const nodeCy = by + 10;
                return (
                  <div key={item.id} className="balloon-layer">
                    <svg className="balloon-anchor" aria-hidden="true">
                      <line x1={nodeCx} y1={nodeCy} x2={targetCx} y2={targetCy} />
                    </svg>
                    <div
                      className="balloon-node"
                      style={{ left: bx, top: by }}
                      onMouseDown={(e) => {
                        e.stopPropagation();
                        const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
                        const canvas = canvasRef.current;
                        if (!canvas) return;
                        const cRect = canvas.getBoundingClientRect();
                        const scaleX = canvas.width / cRect.width;
                        const scaleY = canvas.height / cRect.height;
                        const localX = (e.clientX - cRect.left) * scaleX;
                        const localY = (e.clientY - cRect.top) * scaleY;
                        const nodeX = (rect.left - cRect.left) * scaleX;
                        const nodeY = (rect.top - cRect.top) * scaleY;
                        setDragBalloon({
                          id: item.id,
                          dx: localX - nodeX,
                          dy: localY - nodeY,
                        });
                      }}
                    >
                      {item.balloonNo}
                    </div>
                  </div>
                );
              })}
        </div>
      </div>
      <p className="hint">
        Hover to snap and click to pick a semantic entity. Drag-box still works for rough region extraction.
      </p>
    </div>
  );
}
