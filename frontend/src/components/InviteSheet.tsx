import { KeyRound, Link2, Shield, UserPlus, UserRound, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { Invite, ShareableLink } from "../api/types";
import { inviteMessage } from "../lib/people";
import { errorText } from "../lib/toast";
import { ShareLink } from "./ShareLink";

const DAYS = [
  { days: 1, label: "1 day" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
];

function useModal(open: boolean) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return ref;
}

/** Create an invite link: optional email and name, role, expiry; then hand the link over. */
export function InviteSheet({ open, from, onClose, onCreated }: { open: boolean; from: string; onClose: () => void; onCreated: (inv: Invite) => void }) {
  const ref = useModal(open);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [admin, setAdmin] = useState(false);
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [made, setMade] = useState<(Invite & { url: string }) | null>(null);

  const reset = () => {
    setEmail("");
    setName("");
    setAdmin(false);
    setDays(7);
    setError(null);
    setMade(null);
  };
  useEffect(() => {
    if (open) reset();
  }, [open]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api.invite({ email: email.trim(), name: name.trim(), is_admin: admin, days });
      setMade(inv);
      onCreated(inv);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const link: ShareableLink | null = made && { url: made.url, expires_at: made.expires_at, email: made.email, name: made.name };
  return (
    <dialog ref={ref} className="sheet-dialog invite-sheet" aria-labelledby="inv-title" onCancel={(e) => (e.preventDefault(), onClose())}>
      {link && made ? (
        <div className="sd-body">
          <header className="sd-head">
            <span className="sd-icon ok" aria-hidden>
              <Link2 size={18} />
            </span>
            <div className="sd-title">
              <h2 id="inv-title">Invite link ready</h2>
              <span>
                {made.email ? `For ${made.name ? `${made.name} (${made.email})` : made.email}` : made.name ? `For ${made.name}` : "Open link"} ·{" "}
                {made.is_admin ? "Admin" : "Member"}
              </span>
            </div>
            <button type="button" className="icon-btn" aria-label="Close" title="Close (Esc)" onClick={onClose}>
              <X size={16} />
            </button>
          </header>
          <div className="sd-pad">
            <ShareLink link={link} message={inviteMessage(link, from)} title="Join Quill" />
          </div>
          <footer className="sd-foot">
            <span className="muted">Send it over chat. Quill doesn't send email.</span>
            <button type="button" className="btn" onClick={reset}>
              Invite someone else
            </button>
            <button type="button" className="btn primary" onClick={onClose}>
              Done
            </button>
          </footer>
        </div>
      ) : (
        <form onSubmit={submit}>
          <header className="sd-head">
            <span className="sd-icon" aria-hidden>
              <UserPlus size={18} />
            </span>
            <div className="sd-title">
              <h2 id="inv-title">Invite someone</h2>
              <span>You get a one-time link to send them</span>
            </div>
            <button type="button" className="icon-btn" aria-label="Cancel" title="Cancel (Esc)" onClick={onClose}>
              <X size={16} />
            </button>
          </header>
          <div className="sd-pad inv-form">
            <label className="field">
              <span>
                Email <em>optional</em>
              </span>
              <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" autoFocus autoComplete="off" />
              <small>{email.trim() ? "They'll sign in with this email." : "Leave empty for a link anyone can use once. They choose their own email."}</small>
            </label>
            <label className="field">
              <span>
                Name <em>optional</em>
              </span>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Priya Raman" autoComplete="off" maxLength={80} />
            </label>
            <fieldset className="field role-pick">
              <legend>Role</legend>
              <RoleOption checked={!admin} onPick={() => setAdmin(false)} icon={<UserRound size={16} />} title="Member" body="Uploads recordings and sees their own meetings." />
              <RoleOption checked={admin} onPick={() => setAdmin(true)} icon={<Shield size={16} />} title="Admin" body="Also invites people, manages accounts and changes settings." />
            </fieldset>
            <div className="field">
              <span id="inv-days">Link expires after</span>
              <div className="segmented" role="radiogroup" aria-labelledby="inv-days">
                {DAYS.map((d) => (
                  <button key={d.days} type="button" role="radio" aria-checked={days === d.days} onClick={() => setDays(d.days)}>
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
            {error && (
              <div className="form-error" role="alert">
                {error}
              </div>
            )}
          </div>
          <footer className="sd-foot">
            <span className="muted">
              <KeyRound size={12} /> They choose their own password.
            </span>
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? "Creating…" : "Create invite link"}
            </button>
          </footer>
        </form>
      )}
    </dialog>
  );
}

function RoleOption({ checked, onPick, icon, title, body }: { checked: boolean; onPick: () => void; icon: ReactNode; title: string; body: string }) {
  return (
    <label className={`role-opt ${checked ? "on" : ""}`}>
      <input type="radio" name="role" checked={checked} onChange={onPick} />
      <span className="role-ico" aria-hidden>
        {icon}
      </span>
      <span>
        <b>{title}</b>
        <span>{body}</span>
      </span>
    </label>
  );
}

/** Shows a link that was just made (a password reset link, or a renewed invite). */
export function LinkSheet({ data, onClose }: { data: { title: string; sub: string; link: ShareableLink; message: string } | null; onClose: () => void }) {
  const ref = useModal(!!data);
  return (
    <dialog ref={ref} className="sheet-dialog link-sheet" aria-labelledby="ls-title" onCancel={(e) => (e.preventDefault(), onClose())}>
      {data && (
        <div className="sd-body">
          <header className="sd-head">
            <span className="sd-icon ok" aria-hidden>
              <Link2 size={18} />
            </span>
            <div className="sd-title">
              <h2 id="ls-title">{data.title}</h2>
              <span>{data.sub}</span>
            </div>
            <button type="button" className="icon-btn" aria-label="Close" title="Close (Esc)" onClick={onClose}>
              <X size={16} />
            </button>
          </header>
          <div className="sd-pad">
            <ShareLink link={data.link} message={data.message} title={data.title} />
          </div>
          <footer className="sd-foot">
            <span className="muted">Send it over chat. Quill doesn't send email.</span>
            <button type="button" className="btn primary" onClick={onClose}>
              Done
            </button>
          </footer>
        </div>
      )}
    </dialog>
  );
}
