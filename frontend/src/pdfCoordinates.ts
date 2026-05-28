import type { PageViewport } from "pdfjs-dist/types/src/display/display_utils";

export type ScreenRect = { x: number; y: number; w: number; h: number };

/** Unscaled PDF page height in user points (from viewport viewBox). */
export function pdfPageHeight(viewport: PageViewport): number {
  const vb = viewport.viewBox;
  return vb[3] - vb[1];
}

/**
 * PyMuPDF/fitz bbox [x0, y0, x1, y1] (origin top-left, y down)
 * → PDF.js rectangle [xMin, yMin, xMax, yMax] (PDF user space, y up).
 */
export function fitzBboxToPdfRect(
  bbox: number[],
  pageHeight: number,
): [number, number, number, number] {
  const [x0, y0, x1, y1] = bbox;
  return [x0, pageHeight - y1, x1, pageHeight - y0];
}

/** Fitz bbox → canvas overlay pixels via PDF.js viewport transform. */
export function pdfBboxToScreen(
  bbox: number[],
  viewport: PageViewport,
): ScreenRect {
  const ph = pdfPageHeight(viewport);
  const pdfRect = fitzBboxToPdfRect(bbox, ph);
  const vpRect = viewport.convertToViewportRectangle(pdfRect);
  const left = Math.min(vpRect[0], vpRect[2]);
  const top = Math.min(vpRect[1], vpRect[3]);
  const right = Math.max(vpRect[0], vpRect[2]);
  const bottom = Math.max(vpRect[1], vpRect[3]);
  return {
    x: left,
    y: top,
    w: right - left,
    h: bottom - top,
  };
}

/** Canvas pixel → fitz/PyMuPDF point for backend API calls. */
export function screenToFitzPoint(
  x: number,
  y: number,
  viewport: PageViewport,
): [number, number] {
  const ph = pdfPageHeight(viewport);
  const [pdfX, pdfY] = viewport.convertToPdfPoint(x, y);
  return [pdfX, ph - pdfY];
}

/** Screen drag rectangle → fitz bbox for extraction API. */
export function screenRectToFitzBbox(
  rect: ScreenRect,
  viewport: PageViewport,
): number[] {
  const corners: [number, number][] = [
    [rect.x, rect.y],
    [rect.x + rect.w, rect.y],
    [rect.x, rect.y + rect.h],
    [rect.x + rect.w, rect.y + rect.h],
  ];
  const fitzPts = corners.map(([sx, sy]) => screenToFitzPoint(sx, sy, viewport));
  const xs = fitzPts.map((p) => p[0]);
  const ys = fitzPts.map((p) => p[1]);
  return [
    Math.min(...xs),
    Math.min(...ys),
    Math.max(...xs),
    Math.max(...ys),
  ];
}
