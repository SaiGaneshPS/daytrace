import { describe, expect, it } from "vitest";
import { blockWhen, NOW_LEAD_MS, nowMark } from "../components/timelineText";

const at = (clock: string) => new Date(`2026-09-28T${clock}`).getTime();
const withSeconds = /\d{1,2}:\d{2}:\d{2}/;

describe("a timeline block's tooltip", () => {
  it("gives a few seconds' use in seconds, with the times to the second", () => {
    const when = blockWhen(at("10:22:48"), at("10:22:53"), 5 / 60);
    expect(when.length).toBe("5 seconds");
    expect(when.times).toMatch(withSeconds);
    expect(when.times.split(" to ")).toHaveLength(2);
  });

  it("never says 0 or 60 seconds", () => {
    expect(blockWhen(at("10:00:00"), at("10:00:00"), 0.001).length).toBe("1 second");
    expect(blockWhen(at("10:00:00"), at("10:00:59"), 0.999).length).toBe("59 seconds");
  });

  it("keeps minutes, and times to the minute, from a minute up", () => {
    const when = blockWhen(at("10:02:00"), at("10:20:00"), 18);
    expect(when.length).toBe("18 min (18m)");
    expect(when.times).not.toMatch(withSeconds);
    expect(blockWhen(at("10:00:00"), at("11:35:00"), 95).length).toBe("95 min (1h 35m)");
  });

  it("has no length for a block the hub gives no minutes (a calendar event)", () => {
    expect(blockWhen(at("09:00:00"), at("10:30:00"), null)).toEqual({ times: expect.any(String), length: null });
    expect(blockWhen(at("09:00:00"), at("09:00:30"), null).times).toMatch(withSeconds);
  });
});

describe("the timeline's now line", () => {
  const now = at("12:19:00");

  it("stays on the clock when everything recorded ended before it", () => {
    expect(nowMark(now, [])).toBe(now);
    expect(nowMark(now, [at("12:18:40"), at("09:00:00")])).toBe(now);
  });

  it("moves up to a block that came in after the clock last moved", () => {
    expect(nowMark(now, [at("12:18:40"), at("12:19:20"), at("12:19:08")])).toBe(at("12:19:20"));
  });

  it("ignores an end far past the clock (a device's wrong clock)", () => {
    expect(nowMark(now, [now + NOW_LEAD_MS + 1_000, at("12:19:05")])).toBe(at("12:19:05"));
    expect(nowMark(now, [now + NOW_LEAD_MS])).toBe(now + NOW_LEAD_MS);
  });
});
