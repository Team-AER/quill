import { Eye, EyeOff } from "lucide-react";
import { useId, useState } from "react";
import { MIN_PASSWORD, passwordStrength } from "../lib/people";

interface Props {
  label: string;
  value: string;
  onChange: (v: string) => void;
  /** "new-password" shows the strength hint and enforces the minimum length. */
  autoComplete: "current-password" | "new-password";
  autoFocus?: boolean;
  large?: boolean;
  required?: boolean;
}

/** Password input with show/hide and, for new passwords, a strength hint instead of a confirm field. */
export function PasswordField({ label, value, onChange, autoComplete, autoFocus, large, required = true }: Props) {
  const [shown, setShown] = useState(false);
  const hintId = useId();
  const isNew = autoComplete === "new-password";
  const s = passwordStrength(value);
  return (
    <label className="field">
      <span>{label}</span>
      <span className="pw-field">
        <input
          className={`input ${large ? "lg" : ""}`}
          type={shown ? "text" : "password"}
          autoComplete={autoComplete}
          autoCapitalize="none"
          spellCheck={false}
          required={required}
          minLength={isNew ? MIN_PASSWORD : undefined}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoFocus={autoFocus}
          aria-describedby={isNew ? hintId : undefined}
        />
        <button
          type="button"
          className="pw-reveal"
          onClick={() => setShown((x) => !x)}
          aria-label={shown ? "Hide password" : "Show password"}
          aria-pressed={shown}
          title={shown ? "Hide password" : "Show password"}
        >
          {shown ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </span>
      {isNew && (
        <span className={`pw-meter s${value ? s.score : "x"}`} id={hintId} aria-live="polite">
          <i />
          <i />
          <i />
          <b>{s.label}</b>
        </span>
      )}
    </label>
  );
}
