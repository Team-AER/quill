import { afterEach, expect, it, vi } from "vitest";
import { momentMotion } from "./motion";
afterEach(() => vi.unstubAllGlobals());
it("settles active cosmetic motion on a live reduction and detaches its listener", async () => {
  const listeners = new Set<(event: { matches: boolean }) => void>();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: (_: string, f: (event: { matches: boolean }) => void) => listeners.add(f), removeEventListener: (_: string, f: (event: { matches: boolean }) => void) => listeners.delete(f) }));
  const cancel = vi.fn();
  const animate = vi.fn(() => ({ finished: new Promise(() => {}), cancel }));
  const stop = momentMotion({ animate } as unknown as Element, [{ opacity: 0 }, { opacity: 1 }]);
  for (const change of listeners) change({ matches: true });
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(listeners.size).toBe(0);
  stop();
  expect(cancel).toHaveBeenCalledTimes(1);
});
it("allows the actual task to continue when animation is absent, throws or is reduced", () => {
  expect(() => momentMotion({} as Element, [])()).not.toThrow();
  const animate = vi.fn(() => { throw Error("Unavailable"); });
  expect(() => momentMotion({ animate } as unknown as Element, [])()).not.toThrow();
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  animate.mockClear();
  momentMotion({ animate } as unknown as Element, []);
  expect(animate).not.toHaveBeenCalled();
});
