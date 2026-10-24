import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Meeting } from "../api/types";
import { Pipeline } from "../screens/meeting/Pipeline";

vi.mock("./router", () => ({ linkClick: () => {} }));

const meeting = { status: "failed", stages: [{ name: "diarize", status: "failed", progress: null, error: "Diarizer exited with code 137" }] } as Meeting;

describe("processing failure recovery", () => {
  it("names the stopped task and retry without diagnosing exit137", () => {
    const html = renderToStaticMarkup(createElement(Pipeline, { meeting, isAdmin: false, onRetry: () => {}, busy: false }));
    expect(html).toContain("Speaker detection stopped.");
    expect(html).toContain("Retry speaker detection");
    expect(html).toContain("<summary>Technical details</summary>");
    expect(html).toContain("Diarizer exited with code 137.");
    expect(html).not.toContain("memory");
  });

  it("directs shared readers to the owner without offering an unauthorized retry", () => {
    const html = renderToStaticMarkup(createElement(Pipeline, { meeting, isAdmin: false, busy: false }));
    expect(html).toContain("The meeting owner can retry this stage.");
    expect(html).not.toContain("<button");
  });
});
