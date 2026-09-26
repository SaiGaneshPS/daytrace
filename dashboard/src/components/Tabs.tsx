// DT-52: tabs for the pages' sub-views (Day / Week / Month and so on).
//
// - A proper tablist: arrow keys move between tabs (wrapping), Home and End jump, only the selected tab is in the
//   tab order, and each panel is labelled by its tab.
// - The underline glides to the selected tab, and the panel slides in from the side you moved to.
// - On touch screens, swiping the panel left or right moves to the next or previous tab.
// - Controlled (`value` + `onChange`) or not.
import { AnimatePresence, motion } from "motion/react";
import { type KeyboardEvent, type ReactNode, useId, useRef, useState } from "react";
import { slideVariants, spring, useMediaQuery } from "../theme/motion";

export type TabItem = { id: string; label: ReactNode; content: ReactNode };

type Props = {
  tabs: TabItem[];
  /** What the tabs choose between, for screen readers ("Time range"). */
  label: string;
  value?: string;
  onChange?: (id: string) => void;
  className?: string;
};

const SWIPE_DISTANCE = 60;
const SWIPE_SPEED = 400;

export default function Tabs({ tabs, label, value, onChange, className }: Props) {
  const base = useId();
  const [own, setOwn] = useState(tabs[0]?.id);
  const [direction, setDirection] = useState(1);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const touch = useMediaQuery("(pointer: coarse)");
  const selectedId = value ?? own;
  const index = Math.max(0, tabs.findIndex((tab) => tab.id === selectedId));
  const selected = tabs[index];

  const choose = (next: number, focus = false) => {
    if (next === index || !tabs[next]) return;
    setDirection(next > index ? 1 : -1);
    if (value === undefined) setOwn(tabs[next].id);
    onChange?.(tabs[next].id);
    if (focus) buttons.current[next]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const last = tabs.length - 1;
    const moves: Record<string, number> = {
      ArrowRight: index === last ? 0 : index + 1,
      ArrowLeft: index === 0 ? last : index - 1,
      Home: 0,
      End: last,
    };
    if (event.key in moves) {
      event.preventDefault();
      choose(moves[event.key], true);
    }
  };

  if (!selected) return null;
  return (
    <div className={`tabs${className ? ` ${className}` : ""}`}>
      <div role="tablist" aria-label={label} className="tablist" onKeyDown={onKeyDown}>
        {tabs.map((tab, i) => {
          const active = i === index;
          return (
            <button
              key={tab.id}
              ref={(button) => {
                buttons.current[i] = button;
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${tab.id}`}
              aria-selected={active}
              aria-controls={`${base}-panel-${tab.id}`}
              tabIndex={active ? 0 : -1}
              className={`tab${active ? " active" : ""}`}
              onClick={() => choose(i)}
            >
              {tab.label}
              {active && <motion.span layoutId={`${base}-underline`} className="tab-underline" transition={spring} />}
            </button>
          );
        })}
      </div>
      <AnimatePresence mode="wait" initial={false} custom={direction}>
        <motion.div
          key={selected.id}
          role="tabpanel"
          id={`${base}-panel-${selected.id}`}
          aria-labelledby={`${base}-tab-${selected.id}`}
          tabIndex={0}
          className="tabpanel"
          custom={direction}
          variants={slideVariants}
          initial="initial"
          animate="enter"
          exit="exit"
          drag={touch && tabs.length > 1 ? "x" : false}
          dragConstraints={{ left: 0, right: 0 }}
          dragElastic={0.25}
          dragDirectionLock
          onDragEnd={(_, info) => {
            if (info.offset.x < -SWIPE_DISTANCE || info.velocity.x < -SWIPE_SPEED) choose(index + 1);
            else if (info.offset.x > SWIPE_DISTANCE || info.velocity.x > SWIPE_SPEED) choose(index - 1);
          }}
        >
          {selected.content}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
