// DT-52: placeholders with a soft shimmer while data loads (static under reduced motion). The container that is
// loading sets aria-busy; skeletons themselves are hidden from screen readers.
import type { CSSProperties } from "react";

type Props = {
  width?: CSSProperties["width"];
  height?: CSSProperties["height"];
  radius?: CSSProperties["borderRadius"];
  className?: string;
};

export default function Skeleton({ width = "100%", height = 16, radius, className }: Props) {
  return (
    <span
      className={`skeleton${className ? ` ${className}` : ""}`}
      style={{ width, height, borderRadius: radius }}
      aria-hidden="true"
    />
  );
}

/** Lines of text; the last one shorter, like a paragraph. */
export function SkeletonText({ lines = 3 }: { lines?: number }) {
  return (
    <span className="skeleton-text" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} width={i === lines - 1 && lines > 1 ? "60%" : "100%"} height={12} />
      ))}
    </span>
  );
}
