import { describe, expect, it } from "vitest";
import { probeReadable } from "./readable";

function fakeFile(size: number, readable: boolean): File {
  return {
    name: "meeting.mp4",
    size,
    slice: () => ({
      arrayBuffer: () =>
        readable ? Promise.resolve(new ArrayBuffer(1)) : Promise.reject(new DOMException("x", "NotReadableError")),
    }),
  } as unknown as File;
}

describe("probeReadable", () => {
  it("accepts a readable file", async () => {
    expect(await probeReadable(fakeFile(1_000_000, true))).toBeNull();
  });
  it("explains cloud-only placeholders", async () => {
    expect(await probeReadable(fakeFile(1_070_557_339, false))).toMatch(/cloud-only placeholder/);
  });
  it("rejects empty files", async () => {
    expect(await probeReadable(fakeFile(0, true))).toMatch(/empty/);
  });
});
