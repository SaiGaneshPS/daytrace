// DT-31: the timeline's times and lengths (its axis, slider and tooltips) and where its "now" line goes. A block's
// length is its own end minus its start (the hub's, to the second), so a few seconds read "5 seconds" and never
// "0.08 min (0m)", and "1 min 30 s" never sits next to a rounded "2m".
import { formatMinutes } from "./StatCard";

/** A time of day in the browser's own format ("10:22 a.m."), to the second when asked ("10:22:48 a.m."). */
export const clock = (ms: number, seconds = false) =>
  new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });

/** How long, exactly: "5 seconds", "1 min 30 s", "18 min"; from an hour, whole minutes: "95 min (1h 35m)". */
export function lengthWords(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  if (seconds < 3600) {
    const rest = seconds % 60;
    return `${Math.floor(seconds / 60)} min${rest ? ` ${rest} s` : ""}`;
  }
  const minutes = Math.round(seconds / 60);
  return `${minutes.toLocaleString()} min (${formatMinutes(minutes)})`;
}

/**
 * When a block ran, to the second when it's under a minute, and how long it ran (null for a calendar event: it's
 * planned, not measured).
 */
export function blockWhen(start: number, end: number, measured: boolean): { times: string; length: string | null } {
  const short = end - start < 60_000;
  return { times: `${clock(start, short)} to ${clock(end, short)}`, length: measured ? lengthWords(end - start) : null };
}

/**
 * Where the "now" line goes: the latest of this device's clock, the newest recorded end, and where the line already
 * was this day. The hub cuts every end at its own clock, so an end past this clock means this device's clock is
 * behind the hub's; and the line never steps back when a later refresh cuts an end shorter.
 */
export function nowMark(now: number, recordedEnds: number[], before?: number): number {
  return recordedEnds.reduce((line, end) => Math.max(line, end), Math.max(now, before ?? now));
}
