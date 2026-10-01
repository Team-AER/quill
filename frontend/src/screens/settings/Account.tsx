import { LogOut, Monitor, Moon, Smartphone, Sun, Tablet } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import type { Session, User } from "../../api/types";
import { PasswordField } from "../../components/PasswordField";
import { PersonAvatar } from "../../components/PersonAvatar";
import { describeDevice, displayName, inline, relativeTime, type DeviceKind } from "../../lib/people";
import { useTheme, type ThemePref } from "../../lib/theme";
import { formatDate } from "../../lib/time";
import { errorText, toast } from "../../lib/toast";

const DEVICE_ICON: Record<DeviceKind, ReactNode> = {
  desktop: <Monitor size={16} />,
  phone: <Smartphone size={16} />,
  tablet: <Tablet size={16} />,
};

export function AccountSection({ user, onUser, onSignedOut, reloadKey }: { user: User; onUser: (u: User) => void; onSignedOut: () => void; reloadKey: number }) {
  return (
    <>
      <Profile user={user} onUser={onUser} />
      <Password />
      <Devices onSignedOut={onSignedOut} reloadKey={reloadKey} />
      <Appearance />
    </>
  );
}

function Profile({ user, onUser }: { user: User; onUser: (u: User) => void }) {
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState(user.email);
  const [current, setCurrent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setName(user.name);
    setEmail(user.email);
  }, [user.name, user.email]);

  const emailChanged = email.trim().toLowerCase() !== user.email;
  const dirty = name.trim() !== user.name || emailChanged;
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const u = await api.updateAccount({ name: name.trim(), ...(emailChanged ? { email: email.trim(), current_password: current } : {}) });
      onUser(u);
      setCurrent("");
      toast(emailChanged ? `You now sign in with ${u.email}` : "Profile saved");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="cardsheet" aria-labelledby="prof-h">
      <div className="profile-head">
        <PersonAvatar user={{ name: name.trim() || user.name, email: user.email }} size={52} />
        <div>
          <h2 id="prof-h">{displayName({ name: name.trim() || user.name, email: user.email })}</h2>
          <span>
            {user.is_admin ? "Admin" : "Member"}
            {user.created_at ? ` · Joined ${formatDate(user.created_at)}` : ""}
          </span>
        </div>
      </div>
      <form className="form-grid" onSubmit={save}>
        <label className="field">
          <span>Name</span>
          <input className="input" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} placeholder="How teammates see you" />
        </label>
        <label className="field">
          <span>Email</span>
          <input className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          <small>You sign in with this. Quill never emails it.</small>
        </label>
        {emailChanged && (
          <div className="span-2 reveal">
            <PasswordField label="Current password, to change your email" value={current} onChange={setCurrent} autoComplete="current-password" />
          </div>
        )}
        {error && (
          <div className="form-error span-2" role="alert">
            {error}
          </div>
        )}
        <div className="form-actions span-2">
          {dirty && (
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setName(user.name);
                setEmail(user.email);
                setError(null);
              }}
            >
              Discard
            </button>
          )}
          <button type="submit" className="btn primary" disabled={!dirty || busy || (emailChanged && !current)}>
            {busy ? "Saving…" : "Save profile"}
          </button>
        </div>
      </form>
    </section>
  );
}

function Password() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState(0); // remounts the fields after a change so show/hide resets

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setForm((f) => f + 1);
      toast(r.signed_out ? `Password changed. Signed out ${r.signed_out} other device${r.signed_out === 1 ? "" : "s"}.` : "Password changed");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="cardsheet" aria-labelledby="pw-h">
      <h2 id="pw-h">Password</h2>
      <p className="sub">Changing it signs you out everywhere except here.</p>
      <form className="form-grid" onSubmit={save} key={form}>
        <PasswordField label="Current password" value={current} onChange={setCurrent} autoComplete="current-password" />
        <PasswordField label="New password" value={next} onChange={setNext} autoComplete="new-password" />
        {error && (
          <div className="form-error span-2" role="alert">
            {error}
          </div>
        )}
        <div className="form-actions span-2">
          <button type="submit" className="btn primary" disabled={busy || !current || next.length < 8}>
            {busy ? "Changing…" : "Change password"}
          </button>
        </div>
      </form>
    </section>
  );
}

function Devices({ onSignedOut, reloadKey }: { onSignedOut: () => void; reloadKey: number }) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const load = useCallback(async () => {
    try {
      setSessions(await api.sessions());
    } catch (e) {
      toast(errorText(e), "error");
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const end = async (s: Session) => {
    try {
      await api.endSession(s.id);
      if (s.current) return onSignedOut();
      setSessions((list) => list?.filter((x) => x.id !== s.id) ?? null);
      toast(`Signed out ${describeDevice(s.user_agent).label}`);
    } catch (e) {
      toast(errorText(e), "error");
    }
  };
  const endOthers = async () => {
    try {
      const r = await api.endOtherSessions();
      setSessions((list) => list?.filter((x) => x.current) ?? null);
      toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? "" : "s"}`);
    } catch (e) {
      toast(errorText(e), "error");
    }
  };
  const others = sessions?.filter((s) => !s.current).length ?? 0;

  return (
    <section className="cardsheet" aria-labelledby="dev-h">
      <div className="cs-head">
        <div>
          <h2 id="dev-h">Where you're signed in</h2>
          <p className="sub">Sessions last 30 days. Sign out of any device you don't recognise.</p>
        </div>
        <button type="button" className="btn sm" onClick={() => void endOthers()} disabled={!others}>
          <LogOut size={13} /> Sign out everywhere else
        </button>
      </div>
      {sessions === null ? (
        <div className="skeleton" style={{ height: 112 }} />
      ) : (
        <ul className="devices">
          {sessions.map((s) => {
            const d = describeDevice(s.user_agent);
            return (
              <li key={s.id} className={s.current ? "current" : undefined}>
                <span className="dev-ico" aria-hidden>
                  {DEVICE_ICON[d.kind]}
                </span>
                <div className="dev-main">
                  <b>
                    {d.label}
                    {s.current && <span className="pill ok">This device</span>}
                  </b>
                  <span>
                    {s.current ? "Active now" : s.last_seen_at ? `Active ${inline(relativeTime(s.last_seen_at))}` : "Not used yet"}
                    {s.ip ? ` · ${s.ip}` : ""}
                    {s.created_at ? ` · Signed in ${inline(formatDate(s.created_at))}` : ""}
                  </span>
                </div>
                <button type="button" className="btn sm ghost" onClick={() => void end(s)}>
                  Sign out
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

const THEMES: { id: ThemePref; label: string; icon: ReactNode }[] = [
  { id: "system", label: "Match system", icon: <Monitor size={14} /> },
  { id: "light", label: "Light", icon: <Sun size={14} /> },
  { id: "dark", label: "Dark", icon: <Moon size={14} /> },
];

function Appearance() {
  const [theme, setTheme] = useTheme();
  return (
    <section className="cardsheet" aria-labelledby="look-h">
      <h2 id="look-h">Appearance</h2>
      <p className="sub">Saved in this browser.</p>
      <div className="segmented" role="radiogroup" aria-labelledby="look-h">
        {THEMES.map((t) => (
          <button key={t.id} type="button" role="radio" aria-checked={theme === t.id} onClick={() => setTheme(t.id)}>
            {t.icon} {t.label}
          </button>
        ))}
      </div>
    </section>
  );
}
