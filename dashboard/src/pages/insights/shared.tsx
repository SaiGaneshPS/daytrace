// DT-34: what every Insights tab shares: the hub's answer for a tab and range, the range's words, and the
// week-over-week chips. Every number is the hub's (GET /insights/{tab}); nothing is added up here.
import { useEffect, useRef } from "react";
import { useApi, usePolling } from "../../api/client";
import type { components } from "../../api/schema";
import { dayMonth, daysBetween, isRealDay, shiftDay } from "../../components/DayPicker";
import { formatMinutes } from "../../components/StatCard";

export type InsightsData = components["schemas"]["InsightsTab"];
export type Series = components["schemas"]["Series"];
export type Metric = components["schemas"]["Metric"];
export type Change = components["schemas"]["Change"];
export type HubTab = InsightsData["tab"];
export type AppDetail = components["schemas"]["AppDetail"];
export type Item = components["schemas"]["Item"];
type WithMetrics = { metrics: Metric[] };

/** The ranges the picker offers, and a custom one as "YYYY-MM-DD..YYYY-MM-DD". */
export const PRESETS = [
  { id: "today", label: "Today" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
] as const;
export const MAX_DAYS = 92; // the hub's limits
export const EARLIEST = "1970-01-01";
const CUSTOM = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/;
const REFRESH_MS = 60_000; // a range with today in it: the hub works it out again at most once a minute

/** The first and last day a range stands for, from today (for the custom range's fields). */
export function spanOf(range: string, today: string): { first: string; last: string } | null {
  if (range === "today") return { first: today, last: today };
  const counted = /^(\d{1,3})d$/.exec(range);
  if (counted) return { first: shiftDay(today, 1 - Number(counted[1])), last: today };
  const custom = CUSTOM.exec(range);
  return custom ? { first: custom[1], last: custom[2] } : null;
}

/** Why a custom range can't be asked for, or null when it can: real days, in order, 1 to 92 of them, by today. */
export function rangeProblem(first: string, last: string, today: string): string | null {
  if (!first || !last) return "Pick both days.";
  if (!isRealDay(first) || !isRealDay(last)) return "Pick days that exist.";
  if (last < first) return "The last day is before the first.";
  if (last > today) return "The range can't go past today.";
  if (first < EARLIEST) return "The range can't start before 1970.";
  if (daysBetween(first, last) > MAX_DAYS) return `Up to ${MAX_DAYS} days at a time.`;
  return null;
}

/** Whether a range is one the hub accepts: a preset, or a custom span with no problem. */
export function validRange(range: string, today: string): boolean {
  if (PRESETS.some((preset) => preset.id === range)) return true;
  const custom = CUSTOM.exec(range);
  return !!custom && rangeProblem(custom[1], custom[2], today) === null;
}

/** What a range covers, in words: "Last 7 days, so far" or "1 Sep to 10 Sep". */
export function rangeWords(data: Pick<InsightsData, "range" | "in_progress">): string {
  const { first, last, label } = data.range;
  const words = /^\d{4}-/.test(label) ? (first === last ? dayMonth(first) : `${dayMonth(first)} to ${dayMonth(last)}`) : label;
  return data.in_progress ? `${words}, so far` : words;
}

export function metric(data: WithMetrics, id: string): Metric | undefined {
  return data.metrics.find((item) => item.id === id);
}

/** A metric's number, or null (no data) and undefined (not in this answer). */
export function valueOf(data: WithMetrics | undefined, id: string): number | null | undefined {
  if (!data) return undefined;
  const found = metric(data, id);
  if (!found) return undefined;
  return typeof found.value === "number" ? found.value : null;
}

/** The hub's answer for one tab and range, shown only once it answers this tab and range (a slower answer for the
 * range picked before never shows under the new one). The hub works out which days a range is, so a new day asks
 * again ("7d" moves at midnight), and so does a minute passing while the range has today in it. */
export function useInsights(tab: HubTab, range: string, tz: string, today: string) {
  const loaded = useApi("/api/v1/insights/{tab}", { path: { tab }, query: { range, tz }, quiet: true });
  const { reload } = loaded;
  const data = loaded.current ? loaded.data : undefined;
  const day = useRef(today);
  useEffect(() => {
    if (day.current === today) return;
    day.current = today;
    reload();
  }, [today, reload]);
  usePolling(data?.in_progress ? REFRESH_MS : null, reload);
  return { data, error: data ? undefined : loaded.error, reload };
}

/** One app's range (DT-55), for the detail drawer: asked for only while an app is open, and shown only once it
 * answers this app and range. */
export function useAppDetail(app: string | null, range: string, tz: string, today: string) {
  const loaded = useApi("/api/v1/insights/apps/detail", { query: { app: app ?? "", range, tz }, enabled: app !== null, quiet: true });
  const { reload } = loaded;
  const data = loaded.current && app !== null ? loaded.data : undefined;
  const day = useRef(today);
  useEffect(() => {
    if (day.current === today) return;
    day.current = today;
    if (app !== null) reload();
  }, [today, reload, app]);
  return { data, error: data ? undefined : loaded.error, reload };
}

/** An amount in a change's unit: "9h 25m", "64", "5 pickups". */
function measure(unit: string, value: number): string {
  if (unit === "minutes") return formatMinutes(value);
  if (unit === "score") return String(Math.round(value));
  const shown = value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  return `${shown} ${shown === "1" ? unit.replace(/s$/, "") : unit}`;
}

function Arrow({ up }: { up: boolean }) {
  return (
    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" fill="currentColor">
      <path d={up ? "M6 2l4 6H2z" : "M6 10L2 4h8z"} />
    </svg>
  );
}

/** A change against the days just before the range: an arrow and how much, green when it went the good way, red
 * when not, and plain for an app that is neither work nor a distraction. Its title has both averages ("Screen time
 * a day: 9h 12m against 9h 25m in the 7 days before"). */
export function ChangeChip({ change, days }: { change: Change | undefined; days: number }) {
  if (!change) return null;
  const size = Math.abs(change.delta);
  const points = Math.round(size); // rounded as it is shown (never -0.5 to "0 points")
  const amount =
    change.unit === "score"
      ? `${points} ${points === 1 ? "point" : "points"}`
      : change.change_pct !== null
        ? `${Math.abs(change.change_pct)}%`
        : measure(change.unit, size);
  const against = `the ${days === 1 ? "day" : `${days} days`} before`;
  const then = measure(change.unit, change.before);
  if (change.direction === "same") {
    return (
      <span className="change change-same" title={`${change.label}: about the same as ${against} (${then})`}>
        <span aria-hidden="true">=</span> about the same as {against}
      </span>
    );
  }
  const up = change.direction === "up";
  const judged = change.better !== "neutral";
  const good = change.direction === change.better;
  const tone = !judged ? "change-neutral" : good ? "change-good" : "change-bad";
  return (
    <span className={`change ${tone}`} title={`${change.label}: ${measure(change.unit, change.now)} against ${then} in ${against}`}>
      <Arrow up={up} />
      <span>
        {up ? "up" : "down"} {amount}
      </span>
      <span className="visually-hidden">
        {" "}
        on {against}{judged ? `, ${good ? "a good change" : "a change the wrong way"}` : ""}
      </span>
    </span>
  );
}

/** The browser's time zone: the hub is asked for days in it. */
export function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
