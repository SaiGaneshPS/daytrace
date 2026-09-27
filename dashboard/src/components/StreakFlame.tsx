// DT-54: a streak's flame. It grows with the run (a week is bigger than a day, a month bigger still, then it
// stays), flickers while lit (still under reduced motion), and dims while today is at risk or the run is out.
// It is decoration: the days are always said in words beside it.
import type { CSSProperties } from "react";

export type FlameState = "lit" | "at_risk" | "out";

/** How big a run's flame is: 0.7 for a day or none, up to 1.3 at 30 days or more. */
export function flameScale(days: number): number {
  return 0.7 + (Math.min(Math.max(days, 0), 30) / 30) * 0.6;
}

export default function StreakFlame({ days, state }: { days: number; state: FlameState }) {
  return (
    <span className={`flame flame-${state}`} style={{ "--flame-scale": flameScale(days) } as CSSProperties} aria-hidden="true">
      <svg viewBox="0 0 32 40">
        <path className="flame-outer" d="M16 1c2 7 11 12 11 23a11 11 0 0 1-22 0c0-5 2.5-8.5 5-11 0 4 1.5 6 3.5 7C12.5 13 13 6 16 1z" />
        <path className="flame-inner" d="M16 17c1.5 3.5 6 6 6 11a6 6 0 0 1-12 0c0-2.5 1.2-4.2 2.5-5.5.2 2 1 3 2 3.5-.6-3.5 0-6.5 1.5-9z" />
      </svg>
    </span>
  );
}
