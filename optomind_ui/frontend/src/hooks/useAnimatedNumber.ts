import { useEffect, useRef, useState } from "react";

/** Smooth count-up/down for metric numbers (~300 ms tween, rAF-based). */
export function useAnimatedNumber(target: number | undefined, durationMs = 300): number | undefined {
  const [display, setDisplay] = useState<number | undefined>(target);
  const fromRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (target === undefined) {
      setDisplay(undefined);
      return;
    }
    const from = fromRef.current;
    if (from === undefined || from === target) {
      fromRef.current = target;
      setDisplay(target);
      return;
    }
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);
      const value = from + (target - from) * eased;
      setDisplay(value);
      fromRef.current = value;
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, durationMs]);

  return display;
}
