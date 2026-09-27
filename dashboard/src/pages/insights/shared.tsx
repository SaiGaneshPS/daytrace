// DT-34: what every Insights tab shares: the hub's answer for a tab and range, the range's words, and the
// week-over-week chips. Every number is the hub's (GET /insights/{tab}); nothing is added up here.
import { useApi } from "../../api/client";
import type { components } from "../../api/schema";
import { shiftDay } from "../../components/DayPicker";
import { formatMinutes } from "../../components/StatCard";

export type InsightsData = components["schemas"]["InsightsTab"];
export type Series = components["schemas"]["Series"];
export type Metric = components["schemas"]["Metric"];
export type Change = components["schemas"]["Change"];
export type HubTab = InsightsData["tab"];

/** The ranges the picker offers, and a custom one as "YYYY-MM-DD..YYYY-MM-DD". */
export const PRESETS = [
  { id: "today", label: "Today" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
] as const;
export const MAX_DAYS = 92; // the hub's limit
const CUSTOM = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/;

/** The first and last day a range stands for, from today (the hub works the same days out in the same zone). */
export function spanOf(range: string, today: string): { first: string; last: string } | null {
  if (range === "today") return { first: today, last: today };
  const counted = /^(\d{1,3})d$/.exec(range);
  if (counted) return { first: shiftDay(today, 1 - Number(counted[1])), last: today };
  const custom = CUSTOM.exec(range);
  return custom ? { first: custom[1], last: custom[2] } : null;
}

/** Days from `first` to `last`, both included. */
export function daysBetween(first: string, last: string): number {
  const [a, b] = [first, last].map((day) => {
    const [year, month, date] = day.split("-").map(Number);
    return Date.UTC(year, month - 1, date);
  });
  return Math.round((b - a) / 86_400_000) + 1;
}

/** Whether a range is one the hub accepts: a preset, or a custom span of 1 to 92 days ending by today. */
export function validRange(range: string, today: string): boolean {
  if (PRESETS.some((preset) => preset.id === range)) return true;
  const span = spanOf(range, today);
  if (!span || !CUSTOM.test(range)) return false;
  const days = daysBetween(span.first, span.last);
  return days >= 1 && days <= MAX_DAYS && span.last <= today;
}

/** "Sat 19" for an x-axis label. */
export function shortDay(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString([], { weekday: "short", day: "numeric" });
}

/** "19 Sep" for a range's ends. */
export function dayMonth(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString([], { day: "numeric", month: "short" });
}

/** What a range covers, in words: "Last 7 days, so far" or "1 Sep to 10 Sep". */
export function rangeWords(data: InsightsData): string {
  const { first, last, label } = data.range;
  const words = /^\d{4}-/.test(label) ? (first === last ? dayMonth(first) : `${dayMonth(first)} to ${dayMonth(last)}`) : label;
  return data.in_progress ? `${words}, so far` : words;
}

export function metric(data: InsightsData, id: string): Metric | undefined {
  return data.metrics.find((item) => item.id === id);
}

/** A metric's number, or null (no data) and undefined (not in this answer). */
export function valueOf(data: InsightsData | undefined, id: string): number | null | undefined {
  if (!data) return undefined;
  const found = metric(data, id);
  if (!found) return undefined;
  return typeof found.value === "number" ? found.value : null;
}

/** The hub's answer for one tab and range, shown only once it is the answer for that range (a slower answer for
 * the range picked before never shows under the new one). */
export function useInsights(tab: HubTab, range: string, tz: string, today: string) {
  const loaded = useApi("/api/v1/insights/{tab}", { path: { tab }, query: { range, tz }, quiet: true });
  const span = spanOf(range, today);
  const data = loaded.data && span && loaded.data.range.first === span.first && loaded.data.range.last === span.last ? loaded.data : undefined;
  return { data, error: data ? undefined : loaded.error, reload: loaded.reload };
}

function Arrow({ up }: { up: boolean }) {
  return (
    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" fill="currentColor">
      <path d={up ? "M6 2l4 6H2z" : "M6 10L2 4h8z"} />
    </svg>
  );
}

/** A change against the days just before the range: an arrow and how much, green when it went the good way. */
export function ChangeChip({ change, days }: { change: Change | undefined; days: number }) {
  if (!change) return null;
  const amount =
    change.unit === "score"
      ? `${Math.abs(Math.round(change.delta))} ${Math.abs(Math.round(change.delta)) === 1 ? "point" : "points"}`
      : change.change_pct !== null
        ? `${Math.abs(change.change_pct)}%`
        : formatMinutes(Math.abs(change.delta));
  const then = change.unit === "score" ? String(Math.round(change.before)) : change.unit === "minutes" ? formatMinutes(change.before) : String(change.before);
  const against = `the ${days === 1 ? "day" : `${days} days`} before`;
  if (change.direction === "same") {
    return (
      <span className="change change-same" title={`About the same as ${against} (${then})`}>
        <span aria-hidden="true">=</span> about the same as {against}
      </span>
    );
  }
  const up = change.direction === "up";
  const good = change.direction === change.better;
  return (
    <span className={`change ${good ? "change-good" : "change-bad"}`} title={`${then} in ${against}`}>
      <Arrow up={up} />
      <span>
        {up ? "up" : "down"} {amount}
      </span>
      <span className="visually-hidden">
        {" "}
        on {against}, {good ? "a good change" : "a change the wrong way"}
      </span>
    </span>
  );
}

/** The browser's time zone: the hub is asked for days in it. */
export function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
