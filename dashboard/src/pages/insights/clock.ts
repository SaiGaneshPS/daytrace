// DT-56: bedtimes and wake times on the hub's scale (minutes after 18:00 the evening before), and which night a
// day stands for. Plain functions: the e2e tests check them directly.

/** "23:30" for minutes after 18:00 the evening before. */
export function clock(sinceSix: number): string {
  const total = Math.round(sinceSix) + 18 * 60;
  return `${String(Math.floor(total / 60) % 24).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

/** A time axis for those minutes that ticks on whole hours (every hour, or every two over a long spread): its ends
 * are the hours around the values. */
export function hourAxis(values: (number | null)[]): { min: number; max: number; interval: number } | null {
  const known = values.filter((value): value is number => value !== null);
  if (!known.length) return null;
  const min = Math.floor(Math.min(...known) / 60) * 60;
  const max = Math.max(min + 60, Math.ceil(Math.max(...known) / 60) * 60);
  return { min, max, interval: max - min > 8 * 60 ? 120 : 60 };
}

/** The night that starts on `day` and ends the next morning, in two short day labels: "Sat 19 into Sun 20". */
export function nightWords(day: string, shortDay: (day: string) => string): string {
  const [year, month, date] = day.split("-").map(Number);
  const next = new Date(Date.UTC(year, month - 1, date + 1)).toISOString().slice(0, 10);
  return `${shortDay(day)} into ${shortDay(next)}`;
}

/** "07:30" for an hour of the day as a number (7.5); 24 is "24:00", the end of the day. */
export function hourOfDay(hours: number): string {
  const total = Math.round(hours * 60);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}
