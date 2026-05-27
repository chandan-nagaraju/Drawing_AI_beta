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
};

type HoveredEntity = {
  data: Record<string, unknown>;
  pdfBbox: number[];
};

export default function PdfSemanticViewer({
  documentId,
  pageNumber,
  onExtract,
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
  const hoverTimerRef = useRef<number | null>(null);
  const hoverReqIdRef = useRef(0);
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);
  const renderGenRef = useRef(0);

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

  return (
    <div className="viewer-panel">
      <div className="viewer-toolbar">
        <label>
          Zoom
          <input
            type="range"
            min={0.5}
            max={3}
            step={0.1}
            value={scale}
            onChange={(e) => setScale(parseFloat(e.target.value))}
          />
          <span className="zoom-value">{Math.round(scale * 100)}%</span>
        </label>
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
        </div>
      </div>
      <p className="hint">
        Hover to snap and click to pick a semantic entity. Drag-box still works for rough region extraction.
      </p>
    </div>
  );
}
