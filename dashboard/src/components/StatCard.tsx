// DT-31: a hero stat: a big number that counts up, its unit, a label, a hint underneath and an "Estimated" badge
// when the hub says some of it was inferred. The number is the hub's; this card only shows it.
import { motion } from "motion/react";
import type { ReactNode } from "react";
import { cardVariants } from "../theme/motion";
import AnimatedNumber from "./AnimatedNumber";
import Skeleton from "./Skeleton";

/** Minutes as "2h 35m", "45m" or "0m" (whole minutes: the hub's numbers, rounded for display). */
export function formatMinutes(minutes: number): string {
  const whole = Math.round(minutes);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** Minutes as words for screen readers and tooltips: "2 hours 35 minutes". */
export function spokenMinutes(minutes: number): string {
  const whole = Math.round(minutes);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  const part = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "s"}`;
  if (!hours) return part(rest, "minute");
  return rest ? `${part(hours, "hour")} ${part(rest, "minute")}` : part(hours, "hour");
}

type Props = {
  label: string;
  /** null: the hub has no data for it; undefined: still loading. */
  value: number | null | undefined;
  format?: (value: number) => string;
  unit?: string;
  hint?: ReactNode;
  estimated?: boolean;
  /** A category color for the card's accent stripe. */
  tone?: string;
  /** Why the number couldn't be loaded (shown instead of a skeleton that would never end). */
  error?: string;
  children?: ReactNode;
};

export default function StatCard({ label, value, format, unit, hint, estimated, tone, error, children }: Props) {
  return (
    <motion.section className={`card stat-card${tone ? ` cat-${tone}` : ""}`} aria-label={label} variants={cardVariants}>
      <p className="stat-label">
        <span>{label}</span>
        {estimated && <span className="badge badge-estimated">Estimated</span>}
      </p>
      {value === undefined && error ? (
        <p className="stat-value stat-empty">Couldn&apos;t load</p>
      ) : value === undefined ? (
        <Skeleton height={38} width="65%" radius={10} />
      ) : value === null ? (
        <p className="stat-value stat-empty">No data yet</p>
      ) : (
        <p className="stat-value">
          <AnimatedNumber value={value} format={format} />
          {unit && <span className="stat-unit">{unit}</span>}
        </p>
      )}
      {value === undefined && error ? <p className="stat-hint">{error}</p> : hint && <p className="stat-hint">{hint}</p>}
      {children}
    </motion.section>
  );
}
