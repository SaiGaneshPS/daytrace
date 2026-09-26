// DT-52: the card every chart and stat sits in: a title, the time range and unit, an info button explaining the
// metric, and an "Estimated" badge when the data says some of it was inferred. While loading it shows a skeleton
// and tells screen readers so. Inside a page it rises into place with the other cards (staggered).
import { motion } from "motion/react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { cardVariants } from "../theme/motion";
import Skeleton from "./Skeleton";

type Props = {
  title: string;
  /** The time the numbers cover ("Today", "Last 7 days"). */
  range?: string;
  unit?: string;
  /** What the metric means and how it is worked out. */
  info?: ReactNode;
  /** Some of the data was inferred (an iPhone app whose close was never seen, an estimated night). */
  estimated?: boolean;
  loading?: boolean;
  /** Extra controls in the header (a menu, a toggle). */
  actions?: ReactNode;
  className?: string;
  children?: ReactNode;
};

const ESTIMATED = "Some of this is estimated, for example an app whose closing time was never recorded.";

function InfoButton({ title, info }: { title: string; info: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    const onPointer = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <span className="info" ref={box}>
      <button
        type="button"
        className="icon-button"
        aria-label={`About ${title}`}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((shown) => !shown)}
      >
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 11v6M12 7.5v.5" strokeLinecap="round" />
        </svg>
      </button>
      <span id={id} role="status" className={`info-pop${open ? " open" : ""}`}>
        {open ? info : null}
      </span>
    </span>
  );
}

export default function ChartCard({ title, range, unit, info, estimated, loading, actions, className, children }: Props) {
  const id = useId();
  return (
    <motion.section
      className={`card chart-card${className ? ` ${className}` : ""}`}
      aria-labelledby={`${id}-title`}
      aria-busy={loading || undefined}
      variants={cardVariants}
    >
      <header className="chart-card-head">
        <div className="chart-card-heading">
          <h2 id={`${id}-title`} className="chart-card-title">
            {title}
          </h2>
          {(range || unit) && (
            <p className="chart-card-meta">
              {range && <span>{range}</span>}
              {unit && <span>{unit}</span>}
            </p>
          )}
        </div>
        <div className="chart-card-tools">
          {estimated && (
            <span className="badge badge-estimated" title={ESTIMATED}>
              Estimated
              <span className="visually-hidden">: {ESTIMATED}</span>
            </span>
          )}
          {info && <InfoButton title={title} info={info} />}
          {actions}
        </div>
      </header>
      <div className="chart-card-body">
        {loading ? (
          <>
            <Skeleton height={180} radius={12} />
            <span className="visually-hidden">Loading {title}</span>
          </>
        ) : (
          children
        )}
      </div>
    </motion.section>
  );
}
