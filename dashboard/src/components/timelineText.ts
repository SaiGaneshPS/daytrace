// DT-31: the words in a timeline block's tooltip: when it ran and for how long. A block under a minute gets its times
// to the second and its length in seconds ("5 seconds"), where "0.08 min (0m)" read as broken.
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
