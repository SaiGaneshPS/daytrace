// DT-52: motion presets shared by every page, so the dashboard feels alive in one consistent way.
//
// - The app wraps everything in <MotionConfig reducedMotion="user">, so motion components drop movement when the
//   system asks for reduced motion (fades stay, slides and scales go). CSS animations (skeleton shimmer, the live
//   dot) are switched off in styles.css, AnimatedNumber shows the final value at once, and celebrate() does nothing.
// - Pages fade up, their cards follow one after another (staggered), tabs slide in the direction you moved.
import type { Transition, Variants } from "motion/react";
import { useCallback, useSyncExternalStore } from "react";

export const EASE: [number, number, number, number] = [0.2, 0.8, 0.2, 1];
export const spring: Transition = { type: "spring", stiffness: 420, damping: 36 };

/** A page: fades up, then its cards follow one after another. */
export const pageVariants: Variants = {
  initial: { opacity: 0, y: 14 },
  enter: { opacity: 1, y: 0, transition: { duration: 0.3, ease: EASE, when: "beforeChildren", staggerChildren: 0.06 } },
  exit: { opacity: 0, y: -8, transition: { duration: 0.15, ease: EASE } },
};

/** A card inside a page (or a list): rises and settles. */
export const cardVariants: Variants = {
  initial: { opacity: 0, y: 18, scale: 0.98 },
  enter: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.35, ease: EASE } },
};

/** A group whose children arrive one after another. */
export const listVariants: Variants = {
  initial: {},
  enter: { transition: { staggerChildren: 0.06 } },
};

/** Tab content sliding in from the side you moved to (custom = -1 or 1). */
export const slideVariants: Variants = {
  initial: (direction: number) => ({ opacity: 0, x: direction * 28 }),
  enter: { opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE } },
  exit: (direction: number) => ({ opacity: 0, x: direction * -28, transition: { duration: 0.15, ease: EASE } }),
};

export const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

const lists = new Map<string, MediaQueryList>();

/** One MediaQueryList per query, kept (not rebuilt on every read). */
function mediaList(query: string): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  let list = lists.get(query);
  if (!list) {
    list = window.matchMedia(query);
    lists.set(query, list);
  }
  return list;
}

function matches(query: string): boolean {
  return mediaList(query)?.matches ?? false;
}

/** Whether a media query matches, kept up to date. Subscribes once per query, not on every render. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (listener: () => void) => {
      const list = mediaList(query);
      list?.addEventListener("change", listener);
      return () => list?.removeEventListener("change", listener);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => matches(query), () => false);
}

export function useReducedMotionPreference(): boolean {
  return useMediaQuery(REDUCED_MOTION);
}

const CONFETTI_COLORS = ["--cat-social", "--cat-video", "--cat-work", "--cat-study", "--cat-comms", "--cat-games"];

/** Confetti, for milestones only (a streak record, a badge). Loaded when first used; nothing under reduced motion. */
export async function celebrate(origin?: { x: number; y: number }): Promise<void> {
  if (matches(REDUCED_MOTION)) return;
  const { default: confetti } = await import("canvas-confetti");
  const style = getComputedStyle(document.documentElement);
  const colors = CONFETTI_COLORS.map((name) => style.getPropertyValue(name).trim()).filter(Boolean);
  await confetti({
    particleCount: 140,
    spread: 80,
    startVelocity: 45,
    ticks: 220,
    origin: origin ?? { x: 0.5, y: 0.65 },
    colors,
    disableForReducedMotion: true,
    zIndex: 1000,
  });
}
