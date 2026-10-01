import { describe, expect, it } from "vitest";
import { describeDevice, displayName, inline, inviteMessage, passwordStrength, personInitials, relativeTime, timeLeft } from "./people";

describe("names", () => {
  it("prefers the name and falls back to the email's local part", () => {
    expect(displayName({ name: " Priya Raman ", email: "p@x.io" })).toBe("Priya Raman");
    expect(displayName({ name: "", email: "sam.okafor@x.io" })).toBe("sam.okafor");
  });
  it("makes initials from either", () => {
    expect(personInitials({ name: "Priya Raman", email: "" })).toBe("PR");
    expect(personInitials({ name: "Cher", email: "" })).toBe("CH");
    expect(personInitials({ name: "", email: "sam.okafor@x.io" })).toBe("SO");
  });
});

describe("describeDevice", () => {
  it.each([
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15", "Safari on macOS", "desktop"],
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 Version/19.0 Mobile/15E148 Safari/604.1", "Safari on iPhone", "phone"],
    ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 Edg/141.0", "Edge on Windows", "desktop"],
    ["Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", "Firefox on Linux", "desktop"],
    ["Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36", "Chrome on Android", "phone"],
    ["", "Unknown device", "desktop"],
  ])("%s", (ua, label, kind) => {
    expect(describeDevice(ua)).toEqual({ label, kind });
  });
});

describe("times", () => {
  const now = new Date("2026-10-01T12:00:00Z");
  it("relativeTime", () => {
    expect(relativeTime("2026-10-01T11:59:30Z", now)).toBe("Just now");
    expect(relativeTime("2026-10-01T11:40:00Z", now)).toBe("20 min ago");
    expect(relativeTime("2026-10-01T09:00:00Z", now)).toBe("3 h ago");
    expect(relativeTime("2026-09-30T10:00:00Z", now)).toBe("Yesterday");
    expect(relativeTime("2026-09-27T10:00:00Z", now)).toBe("4 days ago");
    expect(relativeTime(null, now)).toBe("");
  });
  it("inline keeps dates capitalised", () => {
    expect(inline("Yesterday")).toBe("yesterday");
    expect(inline("Today 09:14")).toBe("today 09:14");
    expect(inline("Just now")).toBe("just now");
    expect(inline("Sep 22")).toBe("Sep 22");
    expect(inline("3 h ago")).toBe("3 h ago");
  });
  it("timeLeft", () => {
    expect(timeLeft("2026-10-08T12:00:00Z", now)).toBe("in 7 days");
    expect(timeLeft("2026-10-02T11:00:00Z", now)).toBe("in 23 h");
    expect(timeLeft("2026-10-01T12:10:00Z", now)).toBe("in 10 min");
    expect(timeLeft("2026-09-30T12:00:00Z", now)).toBe("");
  });
});

describe("passwordStrength", () => {
  it("counts down to the minimum", () => {
    expect(passwordStrength("")).toEqual({ score: 0, label: "At least 8 characters" });
    expect(passwordStrength("abcdefg").label).toBe("1 more character");
  });
  it("grades long and varied passwords higher", () => {
    expect(passwordStrength("aaaaaaaaaa").score).toBe(1);
    expect(passwordStrength("password123").score).toBe(1);
    expect(passwordStrength("meetings").score).toBe(1);
    expect(passwordStrength("Meetings42").score).toBe(2);
    expect(passwordStrength("correct horse battery").score).toBe(3);
  });
});

describe("messages", () => {
  it("writes an invite note with the link and first name", () => {
    const m = inviteMessage({ url: "https://q/invite/abc", expires_at: "2026-10-08T12:00:00Z", name: "Priya Raman" }, "Ada");
    expect(m).toMatch(/^Hi Priya, Ada invited you to Quill/);
    expect(m).toContain("https://q/invite/abc");
  });
});
