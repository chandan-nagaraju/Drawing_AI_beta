export type UploadResponse = {
  document_id: string;
  filename: string;
  page_count: number;
  page_width: number;
  page_height: number;
};

export type ExtractResponse = {
  document_id: string;
  page: number;
  region_bbox: number[];
  page_width: number;
  page_height: number;
  entity_count: number;
  entities: Record<string, unknown>[];
};

export type SnapResponse = {
  document_id: string;
  page: number;
  point: number[];
  radius: number;
  region_bbox: number[];
  candidate_count: number;
  selected: (Record<string, unknown> & { text_bbox?: number[] }) | null;
};

const API_BASE = "/api";

export async function uploadPdf(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_BASE}/documents/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? res.statusText);
  }
  return res.json();
}

export function pdfFileUrl(documentId: string): string {
  return `${API_BASE}/documents/${documentId}/file`;
}

/** Load PDF as bytes (avoids PDF.js range/stream issues through the Vite dev proxy). */
export async function fetchPdfBytes(documentId: string): Promise<ArrayBuffer> {
  const res = await fetch(pdfFileUrl(documentId));
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(
      (err as { detail?: string }).detail ??
        `Failed to load PDF (${res.status})`,
    );
  }
  if (res.status === 204) {
    throw new Error("PDF file returned empty (204). Re-upload the drawing.");
  }
  const buf = await res.arrayBuffer();
  if (buf.byteLength < 64) {
    throw new Error("PDF file is empty. Re-upload the drawing.");
  }
  return buf;
}

export async function extractRegion(
  documentId: string,
  page: number,
  bbox: number[],
  glyphMode: "intersects" | "center" = "intersects",
): Promise<ExtractResponse> {
  const res = await fetch(`${API_BASE}/documents/${documentId}/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, bbox, glyph_mode: glyphMode }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? res.statusText);
  }
  return res.json();
}

export async function snapEntity(
  documentId: string,
  page: number,
  x: number,
  y: number,
  radius = 24,
): Promise<SnapResponse> {
  const res = await fetch(`${API_BASE}/documents/${documentId}/snap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, x, y, radius }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? res.statusText);
  }
  return res.json();
}
