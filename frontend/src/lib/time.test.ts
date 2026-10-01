import { describe, expect, it } from "vitest";
import { estimateEta, formatBytes, formatDuration, formatTimestamp, parseTimestamp } from "./time";

describe("formatTimestamp", () => {
  it("formats minutes and seconds", () => {
    expect(formatTimestamp(0)).toBe("0:00");
    expect(formatTimestamp(5.9)).toBe("0:05");
    expect(formatTimestamp(75.4)).toBe("1:15");
    expect(formatTimestamp(599)).toBe("9:59");
  });
  it("adds hours when needed", () => {
    expect(formatTimestamp(3600)).toBe("1:00:00");
    expect(formatTimestamp(3725)).toBe("1:02:05");
    expect(formatTimestamp(65, { forceHours: true })).toBe("0:01:05");
  });
  it("clamps bad input", () => {
    expect(formatTimestamp(-3)).toBe("0:00");
    expect(formatTimestamp(NaN)).toBe("0:00");
    expect(formatTimestamp(null)).toBe("0:00");
  });
  it("round-trips through parseTimestamp", () => {
    for (const s of [0, 59, 61, 3599, 3600, 28800]) expect(parseTimestamp(formatTimestamp(s))).toBe(s);
    expect(parseTimestamp("1:x")).toBeNull();
  });
});

describe("formatDuration", () => {
  it("is human friendly", () => {
    expect(formatDuration(12)).toBe("12 s");
    expect(formatDuration(95)).toBe("1 min 35 s");
    expect(formatDuration(1800)).toBe("30 min");
    expect(formatDuration(3600)).toBe("1 h");
    expect(formatDuration(3725)).toBe("1 h 2 min");
    expect(formatDuration(null)).toBe("–");
  });
});

describe("estimateEta", () => {
  it("extrapolates linearly", () => {
    expect(estimateEta(0.25, 60)).toBeCloseTo(180);
    expect(estimateEta(0.5, 100)).toBeCloseTo(100);
  });
  it("returns null when unknowable", () => {
    expect(estimateEta(0, 60)).toBeNull();
    expect(estimateEta(1, 60)).toBeNull();
    expect(estimateEta(null, 60)).toBeNull();
    expect(estimateEta(0.5, 1)).toBeNull();
  });
});

describe("formatBytes", () => {
  it("scales units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(10 * 1024 ** 3)).toBe("10.0 GB");
  });
});
