import { describe, expect, it } from "vitest";
import { titleFromFilename } from "./title";

describe("titleFromFilename", () => {
  it("drops Teams boilerplate and timestamps", () => {
    expect(titleFromFilename("Discussion with Sales Team-20230208_145424-Meeting Recording.mp4")).toBe("Discussion with Sales Team");
  });
  it("drops dates and capitalises", () => {
    expect(titleFromFilename("2026-09-30 roadmap sync.mp4")).toBe("Roadmap sync");
    expect(titleFromFilename("standup_2026-10-01.m4a")).toBe("Standup");
  });
  it("turns slugs into words", () => {
    expect(titleFromFilename("infra-weekly.mp4")).toBe("Infra weekly");
    expect(titleFromFilename("acme_onboarding_call.mov")).toBe("Acme onboarding call");
  });
  it("keeps titles that are already fine", () => {
    expect(titleFromFilename("E2E test - AMI remote control kickoff.mp4")).toBe("E2E test - AMI remote control kickoff");
    expect(titleFromFilename("Q4 platform roadmap sync.mkv")).toBe("Q4 platform roadmap sync");
  });
  it("falls back sensibly when nothing descriptive is left", () => {
    expect(titleFromFilename("GMT20230208-145424_Recording_1920x1080.mp4")).toBe("Recording");
    expect(titleFromFilename("20230208.wav")).toBe("20230208");
  });
});
