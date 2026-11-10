// Unit tests for the CSV builder (GitHub issue #11).

import { afterEach, describe, expect, it, vi } from 'vitest';
import { downloadCsv, escapeCsvCell, toCsv } from '../csv';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('escapeCsvCell', () => {
  it('returns empty string for null/undefined', () => {
    expect(escapeCsvCell(null)).toBe('');
    expect(escapeCsvCell(undefined)).toBe('');
  });

  it('passes through plain values unchanged', () => {
    expect(escapeCsvCell('normal')).toBe('normal');
    expect(escapeCsvCell(42)).toBe('42');
    expect(escapeCsvCell(true)).toBe('true');
  });

  it('quotes values containing comma, quote, CR or LF', () => {
    expect(escapeCsvCell('a,b')).toBe('"a,b"');
    expect(escapeCsvCell('he said "hi"')).toBe('"he said ""hi"""');
    expect(escapeCsvCell('line1\nline2')).toBe('"line1\nline2"');
    expect(escapeCsvCell('line1\r\nline2')).toBe('"line1\r\nline2"');
  });
});

describe('toCsv', () => {
  it('joins headers and rows with CRLF', () => {
    const csv = toCsv(
      ['time', 'robot', 'rule', 'severity', 'ack'],
      [
        ['2026-01-01T00:00:00Z', 'humanoid-001', 'motor_temp', 3, 'false'],
        ['2026-01-01T00:01:00Z', 'humanoid-002', 'battery_low', 2, 'true'],
      ],
    );
    const lines = csv.split('\r\n');
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe('time,robot,rule,severity,ack');
    expect(lines[1]).toBe('2026-01-01T00:00:00Z,humanoid-001,motor_temp,3,false');
    expect(lines[2]).toBe('2026-01-01T00:01:00Z,humanoid-002,battery_low,2,true');
  });

  it('escapes cells that would break the CSV shape', () => {
    const csv = toCsv(['a', 'b'], [['comma,here', 'quote"x']]);
    expect(csv).toBe('a,b\r\n"comma,here","quote""x"');
  });

  it('emits a header-only CSV for an empty row set', () => {
    expect(toCsv(['x', 'y'], [])).toBe('x,y');
  });
});

describe('downloadCsv', () => {
  it('creates a blob, an anchor, clicks it, and revokes the URL', () => {
    const createObjectURL = vi.fn((_b: Blob) => 'blob:fake');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    downloadCsv('alerts-x.csv', 'time,robot\r\n2026,z');

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blobArg = createObjectURL.mock.calls[0][0];
    expect(blobArg.type).toBe('text/csv;charset=utf-8;');
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake');
  });

  it('is a no-op when there is no DOM (SSR / test env)', () => {
    vi.stubGlobal('document', undefined);
    // Should not throw.
    expect(() => downloadCsv('x.csv', 'a,b')).not.toThrow();
  });
});
