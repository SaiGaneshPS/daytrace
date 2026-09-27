// DT-54: a goal's ring: how far toward a target (or how much of a limit is used), 0 to 100, with the numbers in
// the middle. The ring is decoration; `label` says the same in words for screen readers.
import type { CSSProperties, ReactNode } from "react";

const RADIUS = 42;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** The stroke's dash offset for a percent (0 to 100, clamped; null draws an empty ring). */
export function ringOffset(percent: number | null): number {
  const clamped = percent === null ? 0 : Math.min(100, Math.max(0, percent));
  return CIRCUMFERENCE * (1 - clamped / 100);
}

type Props = { percent: number | null; tone: string; label: string; children?: ReactNode };

export default function ProgressRing({ percent, tone, label, children }: Props) {
  return (
    <div className="ring" role="img" aria-label={label} style={{ "--ring": `var(--cat-${tone})` } as CSSProperties}>
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle className="ring-track" cx="50" cy="50" r={RADIUS} />
        <circle
          className="ring-value"
          cx="50"
          cy="50"
          r={RADIUS}
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={ringOffset(percent)}
          transform="rotate(-90 50 50)"
        />
      </svg>
      <div className="ring-center" aria-hidden="true">
        {children}
      </div>
    </div>
  );
}
