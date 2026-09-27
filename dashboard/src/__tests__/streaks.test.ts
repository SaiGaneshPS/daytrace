// DT-54: the pure parts of the Streaks and Wrapped pages: ISO weeks (a week of the address, the week before and after,
// across a year's end and 53-week years), a streak in words, its flame, a goal's ring, and which badges to celebrate.
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { toCelebrate } from "../components/BadgeShelf";
import { ringOffset } from "../components/ProgressRing";
import { flameScale } from "../components/StreakFlame";
import { amountWords, flameState, todayWords } from "../components/streakText";
import { isoWeekOf, isWeek, shiftWeek, weekMonday } from "../pages/Wrapped";

type Streak = components["schemas"]["Streak"];
type Achievement = components["schemas"]["Achievement"];

describe("ISO weeks", () => {
  it("name the week a day is in, Monday to Sunday", () => {
    expect(isoWeekOf("2026-09-14")).toBe("2026-W38"); // a Monday
    expect(isoWeekOf("2026-09-20")).toBe("2026-W38"); // its Sunday
    expect(isoWeekOf("2026-09-21")).toBe("2026-W39");
  });

  it("belong to the year of their Thursday", () => {
    expect(isoWeekOf("2026-01-01")).toBe("2026-W01"); // a Thursday
    expect(isoWeekOf("2025-12-29")).toBe("2026-W01"); // its Monday, still in 2025
    expect(isoWeekOf("2027-01-01")).toBe("2026-W53"); // 2026 starts on a Thursday: 53 weeks
    expect(isoWeekOf("2027-01-04")).toBe("2027-W01");
    expect(isoWeekOf("2021-01-03")).toBe("2020-W53");
  });

  it("start on the Monday named, and step across the year's end", () => {
    expect(weekMonday("2026-W38")).toBe("2026-09-14");
    expect(weekMonday("2026-W01")).toBe("2025-12-29");
    expect(weekMonday("2026-W53")).toBe("2026-12-28");
    expect(shiftWeek("2026-W53", 1)).toBe("2027-W01");
    expect(shiftWeek("2027-W01", -1)).toBe("2026-W53");
    expect(shiftWeek("2026-W38", -1)).toBe("2026-W37");
    for (let day = new Date(Date.UTC(2020, 0, 1)); day < new Date(Date.UTC(2030, 0, 1)); day.setUTCDate(day.getUTCDate() + 1)) {
      const week = isoWeekOf(day.toISOString().slice(0, 10));
      expect(isoWeekOf(weekMonday(week))).toBe(week); // every day's week starts on its own Monday
    }
  });

  it("from the address must be real weeks", () => {
    expect(isWeek("2026-W53")).toBe(true);
    expect(isWeek("2025-W53")).toBe(false); // 2025 has 52
    expect(isWeek("2026-W00")).toBe(false);
    expect(isWeek("2026-W54")).toBe(false);
    expect(isWeek("2026-w38")).toBe(false);
    expect(isWeek("soon")).toBe(false);
  });
});

const streak = (fields: Partial<Streak>): Streak => ({
  id: "focus_flame", name: "Focus flame", rule: "240 or more focused minutes in a day", needs: "a computer's data for the day",
  kind: "at_least", unit: "minutes", target: 240, current: 3, best: 5, today: "met", value: 300, remaining: null,
  counted: [], best_dates: [], days: [], estimated: false, ...fields,
});

describe("a streak in words", () => {
  it("says an amount in its unit", () => {
    expect(amountWords(45, "minutes")).toBe("45m");
    expect(amountWords(125.4, "minutes")).toBe("2h 5m");
    expect(amountWords(1, "meals")).toBe("1 meal");
    expect(amountWords(2, "meals")).toBe("2 meals");
    expect(amountWords(1.5, "devices")).toBe("1.5 devices");
  });

  it("says what today needs, for a target and for a limit", () => {
    expect(todayWords(streak({ today: "met" }))).toBe("Done for today");
    expect(todayWords(streak({ today: "missed" }))).toBe("Missed today");
    expect(todayWords(streak({ today: "no_data" }))).toBe("Nothing yet today: it needs a computer's data for the day");
    expect(todayWords(streak({ today: "at_risk", remaining: { value: 45, unit: "minutes" } }))).toBe("45m more to go today");
    expect(todayWords(streak({ today: "at_risk", kind: "at_most", remaining: { value: 42.02, unit: "minutes" } }))).toBe("42m left under the limit today");
    expect(todayWords(streak({ today: "at_risk", remaining: null }))).toBe("Not done yet today");
    expect(todayWords(streak({ today: "at_risk", unit: "meals", remaining: { value: 1, unit: "meals" } }))).toBe("1 more meal to go today");
    expect(todayWords(streak({ today: "at_risk", unit: "devices", remaining: { value: 2, unit: "devices" } }))).toBe("2 more devices to go today");
  });

  it("lights its flame while the run is going, dims it while today is at risk", () => {
    expect(flameState(streak({ current: 3, today: "met" }))).toBe("lit");
    expect(flameState(streak({ current: 3, today: "no_data" }))).toBe("lit"); // a day without data never breaks it
    expect(flameState(streak({ current: 3, today: "at_risk" }))).toBe("at_risk");
    expect(flameState(streak({ current: 0, today: "at_risk" }))).toBe("out");
    expect(flameState(streak({ current: 0, today: "missed" }))).toBe("out");
  });

  it("grows its flame with the run, up to a month", () => {
    expect(flameScale(0)).toBeCloseTo(0.7);
    expect(flameScale(-2)).toBeCloseTo(0.7);
    expect(flameScale(7)).toBeGreaterThan(flameScale(1));
    expect(flameScale(30)).toBeCloseTo(1.3);
    expect(flameScale(300)).toBeCloseTo(1.3);
  });
});

describe("a goal's ring", () => {
  it("fills with the percent, within 0 and 100", () => {
    const whole = ringOffset(0);
    expect(ringOffset(100)).toBeCloseTo(0);
    expect(ringOffset(30)).toBeCloseTo(whole * 0.7);
    expect(ringOffset(140)).toBeCloseTo(0);
    expect(ringOffset(-5)).toBeCloseTo(whole);
    expect(ringOffset(null)).toBeCloseTo(whole); // no data: an empty ring
  });
});

describe("badges to celebrate", () => {
  const badge = (id: string, unlocked: boolean) => ({ id, name: id, rule: "", unlocked, earned_on: null, unlocked_at: null, dates: [], progress: null }) as Achievement;
  it("are the unlocked ones this browser hasn't celebrated", () => {
    const badges = [badge("first_sync", true), badge("streak_7", true), badge("streak_30", false)];
    expect(toCelebrate(badges, new Set())).toEqual(["first_sync", "streak_7"]);
    expect(toCelebrate(badges, new Set(["first_sync"]))).toEqual(["streak_7"]);
    expect(toCelebrate(badges, new Set(["first_sync", "streak_7"]))).toEqual([]);
  });
});
