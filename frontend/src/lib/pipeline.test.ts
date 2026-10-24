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

describe("paused processing status", () => {
  const paused = (detail: string) => ({ status: "paused", stages: [{ name: "transcribe", status: "paused", progress: 0.42, detail }] }) as Meeting;
  it("summarizes only known model diagnostics and preserves exact detail", () => {
    const html = renderToStaticMarkup(createElement(Pipeline, { meeting: paused("STT paused: model disabled in llm-proxy"), isAdmin: true, busy: false }));
    expect(html).toContain("Transcription paused.");
    expect(html).toContain("Speech model unavailable.");
    expect(html).toContain("Quill retries automatically.");
    expect(html).toContain("STT paused: model disabled in llm-proxy.");
    expect(html).toContain("Models");
    expect(html).not.toContain("nothing is lost");
  });
  it("does not diagnose unknown pauses or grant shared readers model controls", () => {
    const html = renderToStaticMarkup(createElement(Pipeline, { meeting: paused("Waiting for storage"), isAdmin: false, busy: false }));
    expect(html).toContain("Waiting to resume.");
    expect(html).toContain("Waiting for storage.");
    expect(html).not.toContain("Speech model unavailable");
    expect(html).not.toContain("href=\"/settings/system\"");
  });
});
