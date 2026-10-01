import { X } from "lucide-react";
import { dismissToast, useToasts } from "../lib/toast";

export function Toasts() {
  const toasts = useToasts();
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind === "error" ? "error" : ""}`}>
          <span>{t.text}</span>
          {t.action && (
            <button
              type="button"
              className="btn sm"
              onClick={() => {
                t.action!.run();
                dismissToast(t.id);
              }}
            >
              {t.action.label}
            </button>
          )}
          <button type="button" className="icon-btn sm" aria-label="Dismiss" onClick={() => dismissToast(t.id)} style={{ color: "inherit" }}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
