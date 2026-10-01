import { UserRound, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import type { User } from "../api/types";
import { usePref } from "../lib/prefs";
import { errorText, toast } from "../lib/toast";

/** Asks people without a display name for one: shared meetings and People show it. */
export function NamePrompt({ user, onUser }: { user: User; onUser: (u: User) => void }) {
  const [later, setLater] = usePref<boolean>(`namePrompt.later.${user.id}`, false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  if (user.name || later) return null;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const u = await api.updateAccount({ name: name.trim() });
      onUser(u);
      toast(`Thanks, ${u.name.split(" ")[0]}`);
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="name-prompt" onSubmit={save} aria-label="Add your name">
      <span className="np-ico" aria-hidden>
        <UserRound size={16} />
      </span>
      <span className="np-text">
        <b>What should teammates call you?</b>
        <span>Your name shows on meetings you share and in People.</span>
      </span>
      <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" aria-label="Your name" autoComplete="name" maxLength={80} />
      <button type="submit" className="btn primary" disabled={busy || !name.trim()}>
        Save
      </button>
      <button type="button" className="icon-btn sm" onClick={() => setLater(true)} aria-label="Not now" title="Not now (you can add it in Settings)">
        <X size={14} />
      </button>
    </form>
  );
}
