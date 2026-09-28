// DT-31: the words in a timeline block's tooltip (when it ran and for how long), and where the "now" line goes. A block
// under a minute gets its times to the second and its length in seconds ("5 seconds"), where "0.08 min (0m)" read as
// broken.
import { formatMinutes } from "./StatCard";

const time = (ms: number, seconds: boolean) =>
  new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });

/** When a block ran ("10:02 a.m. to 10:20 a.m.") and how long ("18 min (18m)", "5 seconds"; null when the hub gives no minutes). */
export function blockWhen(start: number, end: number, minutes: number | null): { times: string; length: string | null } {
  const short = minutes === null ? end - start < 60_000 : minutes < 1;
  const times = `${time(start, short)} to ${time(end, short)}`;
  if (minutes === null) return { times, length: null };
  if (minutes < 1) {
    const seconds = Math.min(59, Math.max(1, Math.round(minutes * 60)));
    return { times, length: `${seconds} second${seconds === 1 ? "" : "s"}` };
  }
  return { times, length: `${minutes.toLocaleString()} min (${formatMinutes(minutes)})` };
}

/** How far past the clock a recorded end may lift the now line: past that it's a device's wrong clock, not a lag. */
export const NOW_LEAD_MS = 2 * 60_000;

/**
 * Where the "now" line goes: the clock, or the newest recorded end when that is a little later. Today moves the line
 * every 30 s, while live mode sends a phone's use every few seconds, so a block that just came in would sit past it.
 */
export function nowMark(now: number, recordedEnds: number[]): number {
  return recordedEnds.reduce((line, end) => (end > line && end <= now + NOW_LEAD_MS ? end : line), now);
}
