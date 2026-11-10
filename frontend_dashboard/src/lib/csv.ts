// CSV builder + browser download helper (GitHub issue #11: export a robot's alert history).
//
// Deliberately tiny and dependency-free: the alert log is small (<= 200 rows), so building the string
// on the client and triggering a blob download is simpler and safer than a backend round-trip, and
// keeps no PII on the server.

export type CsvCell = string | number | boolean | null | undefined;

/** Escape a single cell per RFC 4180: quote when it contains comma, quote, CR or LF. */
export function escapeCsvCell(value: CsvCell): string {
  if (value === null || value === undefined) return '';
  const s = String(value);
  if (/[",\r\n]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

/**
 * Build a CSV string from a header row and an array of row arrays.
 * Uses CRLF line endings (Excel/Sheets open it correctly on every OS).
 */
export function toCsv(headers: string[], rows: CsvCell[][]): string {
  const lines: string[] = [headers.map(escapeCsvCell).join(',')];
  for (const row of rows) {
    lines.push(row.map(escapeCsvCell).join(','));
  }
  return lines.join('\r\n');
}

/**
 * Trigger a browser download of `content` as a .csv file.
 * Safe no-op outside a DOM (e.g. SSR / test env without `document`).
 */
export function downloadCsv(filename: string, content: string): void {
  if (typeof document === 'undefined' || typeof URL === 'undefined') return;
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
