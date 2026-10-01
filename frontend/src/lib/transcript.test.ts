import { describe, expect, it } from "vitest";
import { findActiveLine, highlightParts, searchLines, speakerBands } from "./transcript";

const lines = [
  { speaker: "S1", start: 0, end: 4, text: "Hello everyone" },
  { speaker: "S1", start: 4.5, end: 9, text: "Let's start" },
  { speaker: "S2", start: 10, end: 14, text: "Sounds good" },
  { speaker: "S1", start: 20, end: 25, text: "Next item: budget" },
];

describe("findActiveLine", () => {
  it("returns -1 before the first line", () => {
    expect(findActiveLine(lines, -1)).toBe(-1);
    expect(findActiveLine([], 5)).toBe(-1);
  });
  it("finds the line containing t", () => {
    expect(findActiveLine(lines, 0)).toBe(0);
    expect(findActiveLine(lines, 5)).toBe(1);
    expect(findActiveLine(lines, 10)).toBe(2);
  });
  it("keeps the previous line through a gap", () => {
    expect(findActiveLine(lines, 16)).toBe(2);
  });
  it("returns the last line past the end", () => {
    expect(findActiveLine(lines, 9999)).toBe(3);
  });
  it("is consistent with a linear scan on a long transcript", () => {
    const many = Array.from({ length: 5000 }, (_, i) => ({ start: i * 3.1, end: i * 3.1 + 2.5 }));
    for (const t of [0, 1.2, 777.7, 15499.9, 20000]) {
      let expected = -1;
      many.forEach((l, i) => {
        if (l.start <= t) expected = i;
      });
      expect(findActiveLine(many, t)).toBe(expected);
    }
  });
});

describe("speakerBands", () => {
  it("merges consecutive same-speaker lines", () => {
    expect(speakerBands(lines)).toEqual([
      { speaker: "S1", start: 0, end: 9 },
      { speaker: "S2", start: 10, end: 14 },
      { speaker: "S1", start: 20, end: 25 },
    ]);
  });
});

describe("search", () => {
  it("matches case-insensitively", () => {
    expect(searchLines(lines, "BUDGET")).toEqual([3]);
    expect(searchLines(lines, "  ")).toEqual([]);
  });
  it("splits highlight parts", () => {
    expect(highlightParts("Budget and budget", "budget")).toEqual([
      { text: "Budget", hit: true },
      { text: " and ", hit: false },
      { text: "budget", hit: true },
    ]);
  });
});
