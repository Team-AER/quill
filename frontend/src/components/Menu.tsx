import { useEffect, useId, useRef, useState, type ReactNode } from "react";

interface Props {
  label: string; // accessible name + tooltip
  button: ReactNode;
  buttonClass?: string;
  children: (close: () => void) => ReactNode;
  align?: "left" | "right";
}

/** Disclosure-style menu: button + popover; arrow keys move focus, Esc closes. */
export function Menu({ label, button, buttonClass = "icon-btn", children, align = "right" }: Props) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = () => {
    setOpen(false);
    btn.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    const first = wrap.current?.querySelector<HTMLElement>(".menu button, .menu a");
    first?.focus();
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const onKey = (e: React.KeyboardEvent) => {
    if (!open) return;
    const els = Array.from(wrap.current?.querySelectorAll<HTMLElement>(".menu button:not(:disabled), .menu a") ?? []);
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      els[(i + 1) % els.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      els[(i - 1 + els.length) % els.length]?.focus();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className="menu-wrap" ref={wrap} onKeyDown={onKey}>
      <button
        ref={btn}
        type="button"
        className={buttonClass}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={id}
        aria-label={label}
        title={label}
        onClick={() => setOpen((o) => !o)}
      >
        {button}
      </button>
      {open && (
        <div className="menu" id={id} style={align === "left" ? { left: 0, right: "auto" } : undefined}>
          {children(close)}
        </div>
      )}
    </div>
  );
}
