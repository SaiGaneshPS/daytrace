import { describe, expect, it } from "vitest";
import { blockWhen, lengthWords, nowMark } from "../components/timelineText";

const at = (clock: string) => new Date(`2026-09-28T${clock}`).getTime();
// The expected times in this machine's own format, whatever its language: the tests check which precision is used.
const minute = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const second = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

describe("a timeline block's tooltip", () => {
  it("gives a few seconds' use in seconds, with the times to the second", () => {
    const [start, end] = [at("10:22:48"), at("10:22:53")];
    expect(blockWhen(start, end, true)).toEqual({ times: `${second(start)} to ${second(end)}`, length: "5 seconds" });
  });

  it("gives times to the minute from a minute up", () => {
    const [start, end] = [at("10:22:00"), at("10:23:30")];
    expect(blockWhen(start, end, true)).toEqual({ times: `${minute(start)} to ${minute(end)}`, length: "1 min 30 s" });
  });

  it("says exactly how long, never a rounded number beside an exact one", () => {
    expect(lengthWords(0)).toBe("0 seconds");
    expect(lengthWords(1_000)).toBe("1 second");
    expect(lengthWords(59_400)).toBe("59 seconds");
    expect(lengthWords(61_000)).toBe("1 min 1 s");
    expect(lengthWords(18 * 60_000)).toBe("18 min");
    expect(lengthWords(59.5 * 60_000)).toBe("59 min 30 s");
    expect(lengthWords(95 * 60_000)).toBe(`${(95).toLocaleString()} min (1h 35m)`);
  });

  it("has no length for a calendar event (planned, not measured)", () => {
    const [start, end] = [at("09:00:00"), at("10:30:00")];
    expect(blockWhen(start, end, false)).toEqual({ times: `${minute(start)} to ${minute(end)}`, length: null });
  });
});

describe("the timeline's now line", () => {
  const now = at("12:19:00");

  it("stays on the clock when everything recorded ended before it", () => {
    expect(nowMark(now, [])).toBe(now);
    expect(nowMark(now, [at("12:18:40"), at("09:00:00")])).toBe(now);
  });

  it("moves up to the newest block, even one far past this clock (this device's clock is behind the hub's)", () => {
    expect(nowMark(now, [at("12:18:40"), at("12:19:20"), at("12:19:08")])).toBe(at("12:19:20"));
    expect(nowMark(now, [at("12:22:05")])).toBe(at("12:22:05"));
  });

  it("never steps back when a later refresh cuts the newest block shorter", () => {
    expect(nowMark(now, [at("12:18:10")], at("12:19:25"))).toBe(at("12:19:25"));
    expect(nowMark(at("12:19:30"), [at("12:18:10")], at("12:19:25"))).toBe(at("12:19:30"));
  });
});
