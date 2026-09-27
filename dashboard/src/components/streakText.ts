// DT-54: a streak in words (the Streaks page and Today's strip say the same): an amount in its unit, what today
// needs, and how its flame looks.
import type { components } from "../api/schema";
import type { FlameState } from "./StreakFlame";
import { formatMinutes } from "./StatCard";

type Streak = components["schemas"]["Streak"];

/** "1 day", "14 days". */
export const dayCount = (count: number) => `${count} ${count === 1 ? "day" : "days"}`;

/** An amount in a streak's or goal's unit: "45m" for minutes, "1 meal", "2 devices". */
export function amountWords(value: number, unit: string): string {
  if (unit === "minutes") return formatMinutes(value);
  const shown = value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  const singular = unit.endsWith("s") ? unit.slice(0, -1) : unit;
  return `${shown} ${shown === "1" ? singular : unit}`;
}

/** "45m more", "1 more meal", "2 more devices". */
function moreWords(value: number, unit: string): string {
  const words = amountWords(value, unit);
  return unit === "minutes" ? `${words} more` : words.replace(" ", " more ");
}

/** What today looks like for a streak, in words. */
export function todayWords(streak: Streak): string {
  switch (streak.today) {
    case "met":
      return "Done for today";
    case "missed":
      return "Missed today";
    case "no_data":
      return `Nothing yet today: it needs ${streak.needs}`;
    default: {
      const left = streak.remaining;
      if (!left) return "Not done yet today";
      if (streak.kind === "at_most") return `${amountWords(left.value, left.unit)} left under the limit today`;
      return `${moreWords(left.value, left.unit)} to go today`;
    }
  }
}

export function flameState(streak: Streak): FlameState {
  if (streak.today === "at_risk") return streak.current > 0 ? "at_risk" : "out";
  return streak.current > 0 ? "lit" : "out";
}
