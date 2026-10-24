// Cosmetic transitions never own media state or completion.
export function momentMotion(element: Element | null, frames: Keyframe[], duration = 420) {
  if (!element || typeof element.animate !== "function") return () => {};
  const media = typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  if (media?.matches) return () => {};
  let animation: Animation | undefined;
  let settled = false;
  const stop = () => {
    if (settled) return;
    settled = true;
    media?.removeEventListener("change", changed);
    animation?.cancel();
  };
  const changed = (event: MediaQueryListEvent) => { if (event.matches) stop(); };
  try {
    animation = element.animate(frames, { duration, easing: "cubic-bezier(.2,.8,.2,1)" });
    animation.finished.then(stop, stop);
    media?.addEventListener("change", changed);
  } catch { stop(); }
  return stop;
}
