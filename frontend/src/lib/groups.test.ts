import { describe, expect, it } from "vitest";
import { groupByDay } from "./groups";

describe("groupByDay", () => {
  const now = new Date(2026, 9, 1, 12, 0); // Thu 1 Oct 2026
  const at = (y: number, m: number, d: number, h = 9) => new Date(y, m, d, h).toISOString();
  it("buckets recent days and months, keeping order", () => {
    const items = [
      { id: "a", at: at(2026, 9, 1) },
      { id: "b", at: at(2026, 8, 30) },
      { id: "c", at: at(2026, 8, 27) },
      { id: "d", at: at(2026, 8, 10) },
      { id: "e", at: at(2026, 8, 2) },
      { id: "f", at: at(2025, 1, 8) },
      { id: "g", at: "" },
    ];
    const g = groupByDay(items, (x) => x.at, now);
    expect(g.map((x) => [x.key, x.items.map((i) => i.id).join("")])).toEqual([
      ["today", "a"],
      ["yesterday", "b"],
      ["week", "c"],
      ["2026-8", "de"],
      ["2025-1", "f"],
      ["undated", "g"],
    ]);
    expect(g[4].label).toMatch(/2025/);
    expect(g[3].label).not.toMatch(/2026/);
  });
});
