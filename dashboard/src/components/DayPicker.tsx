// DT-33: the day picker and local-date helpers, shared by Today (DT-31), Story, Ask and Insights (DT-34). Days are
// YYYY-MM-DD in the browser's time zone, and "today" follows midnight while the page is open.
import { useEffect, useState } from "react";

/** YYYY-MM-DD for a local date. */
export function isoDay(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Whether `day` is a real YYYY-MM-DD date (not 2026-02-30). */
export function isRealDay(day: string): boolean {
  const found = ISO_DAY.exec(day);
  if (!found) return false;
  const [year, month, date] = found.slice(1).map(Number);
  const moment = new Date(Date.UTC(year, month - 1, date));
  return moment.getUTCFullYear() === year && moment.getUTCMonth() === month - 1 && moment.getUTCDate() === date;
}

/** Days from `first` to `last`, both included. */
export function daysBetween(first: string, last: string): number {
  const [a, b] = [first, last].map((day) => {
    const [year, month, date] = day.split("-").map(Number);
    return Date.UTC(year, month - 1, date);
  });
  return Math.round((b - a) / 86_400_000) + 1;
}

export function shiftDay(day: string, by: number): string {
  const [year, month, date] = day.split("-").map(Number);
  return isoDay(new Date(year, month - 1, date + by));
}

export function longDay(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

/** "Sat 19" for an axis label; anything that isn't a YYYY-MM-DD day stays as it is. */
export function shortDay(day: string): string {
  if (!ISO_DAY.test(day)) return day;
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString([], { weekday: "short", day: "numeric" });
}

/** "19 Sep" (or "Sep 19", as the browser's language has it) for a range's ends. */
export function dayMonth(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).toLocaleDateString([], { day: "numeric", month: "short" });
}

/** Today's date, kept up to date across midnight. */
export function useToday(): string {
  const [today, setToday] = useState(() => isoDay(new Date()));
  useEffect(() => {
    const timer = window.setInterval(() => setToday(isoDay(new Date())), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return today;
}

export function Chevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
      <path d={direction === "left" ? "M15 5l-7 7 7 7" : "M9 5l7 7-7 7"} />
    </svg>
  );
}

export default function DayPicker({ day, today, onChange }: { day: string; today: string; onChange: (day: string) => void }) {
  return (
    <div className="day-picker">
      <div className="day-step">
      <button type="button" className="icon-button" aria-label="Previous day" onClick={() => onChange(shiftDay(day, -1))}>
        <Chevron direction="left" />
      </button>
      <input
        type="date"
        className="date-input"
        aria-label="Day"
        value={day}
        max={today}
        onChange={(event) => event.target.value && onChange(event.target.value > today ? today : event.target.value)}
      />
      <button
        type="button"
        className="icon-button"
        aria-label="Next day"
        disabled={day >= today}
        onClick={() => onChange(shiftDay(day, 1))}
      >
        <Chevron direction="right" />
      </button>
      </div>
      {day !== today && (
        <button type="button" className="button button-ghost" onClick={() => onChange(today)}>
          Today
        </button>
      )}
    </div>
  );
}
