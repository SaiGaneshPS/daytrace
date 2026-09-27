// DT-52: a number that counts up to its value (and on to new values), for the big stats. Screen readers get the
// final value straight away; with reduced motion everyone does.
import { animate } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { EASE, useReducedMotionPreference } from "../theme/motion";

type Props = {
  value: number;
  /** Digits after the point (default 0). The values shown while counting are rounded to these too, so `format`
   * only ever sees numbers like the final one (whole minutes stay whole). */
  decimals?: number;
  format?: (value: number) => string;
  /** Seconds (default 0.8). */
  duration?: number;
  className?: string;
};

export default function AnimatedNumber({ value, decimals = 0, format, duration = 0.8, className }: Props) {
  const reduced = useReducedMotionPreference();
  const [counted, setCounted] = useState(reduced ? value : 0);
  const from = useRef(reduced ? value : 0);

  useEffect(() => {
    if (reduced) {
      from.current = value;
      return;
    }
    const controls = animate(from.current, value, {
      duration,
      ease: EASE,
      onUpdate: (current) => {
        from.current = current;
        setCounted(current);
      },
    });
    return () => controls.stop();
  }, [value, reduced, duration]);

  const scale = 10 ** decimals;
  const round = (number: number) => Math.round(number * scale) / scale;
  const text = (number: number) =>
    format ? format(round(number)) : round(number).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  // Under reduced motion the value itself is shown, not a counter that would catch up a frame later.
  const shown = reduced ? value : counted;
  return (
    <span className={`number${className ? ` ${className}` : ""}`}>
      <span aria-hidden="true">{text(shown)}</span>
      <span className="visually-hidden">{text(value)}</span>
    </span>
  );
}
