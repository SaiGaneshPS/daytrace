// DT-36: the Privacy page's pure parts: sizes as people say them, and words read the way the hub reads them (its own
// answers for these samples are in the e2e fixture, written and checked by the hub's tests).
import { describe, expect, it } from "vitest";
import fixture from "../../e2e/fixtures/privacy.json";
import { sizeText, wordKey } from "../pages/Privacy";

describe("sizes", () => {
  it("are said in the unit they round to", () => {
    expect(sizeText(0)).toBe("1 KB");
    expect(sizeText(500)).toBe("1 KB");
    expect(sizeText(421_888)).toBe("412 KB");
    expect(sizeText(1_048_300)).toBe("1.0 MB"); // not "1024 KB"
    expect(sizeText(13_002_342)).toBe("12.4 MB");
    expect(sizeText(1_073_700_000)).toBe("1.00 GB"); // not "1024.0 MB"
    expect(sizeText(5_368_709_120)).toBe("5.00 GB");
  });
});

describe("words", () => {
  it("are read as the hub reads them", () => {
    const hub = fixture.tokens as Record<string, string[]>;
    expect(Object.keys(hub).length).toBeGreaterThan(5);
    for (const [word, tokens] of Object.entries(hub)) {
      expect(wordKey(word), word).toBe(tokens.join(" "));
    }
  });

  it("are the same word whatever the case or the marks between letters", () => {
    expect(wordKey("e-mail")).toBe(wordKey("E mail"));
    expect(wordKey("--")).toBe(""); // nothing to look for
  });
});
