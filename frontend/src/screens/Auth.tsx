import { CircleAlert, Clock, Feather, Shield } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api } from "../api/client";
import type { InvitePreview, ResetPreview, User } from "../api/types";
import { PasswordField } from "../components/PasswordField";
import { PersonAvatar } from "../components/PersonAvatar";
import { displayName, timeLeft } from "../lib/people";
import { navigate } from "../lib/router";
import { errorText, toast } from "../lib/toast";

function Card({ title, sub, children, icon }: { title: string; sub: ReactNode; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="auth-wrap">
      <div className="auth-card">
        {icon ?? (
          <span className="brand-mark" aria-hidden>
            <Feather size={22} />
          </span>
        )}
        <div>
          <h1>{title}</h1>
          <p>{sub}</p>
        </div>
        {children}
      </div>
    </div>
  );
}

function useSubmit(fn: () => Promise<unknown>, done: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await fn();
      done();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, submit };
}

const ErrorLine = ({ error }: { error: string | null }) =>
  error ? (
    <div className="form-error" role="alert">
      {error}
    </div>
  ) : null;

export function Login({ onDone, notice }: { onDone: () => void; notice?: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [forgot, setForgot] = useState(false);
  const { busy, error, submit } = useSubmit(() => api.login(email, password), onDone);
  return (
    <Card title="Sign in to Quill" sub="Meeting transcripts and notes for Team AER.">
      {notice && !error && <div className="auth-notice">{notice}</div>}
      <form onSubmit={submit} className="auth-form">
        <label className="field">
          <span>Email</span>
          <input className="input lg" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </label>
        <PasswordField label="Password" value={password} onChange={setPassword} autoComplete="current-password" large />
        <ErrorLine error={error} />
        <button className="btn primary lg" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <div className="auth-foot">
        <button type="button" className="linkish" aria-expanded={forgot} onClick={() => setForgot((f) => !f)}>
          Forgot your password?
        </button>
        {forgot && (
          <p className="auth-help">
            Quill doesn't send email. Ask an admin of this Quill for a password reset link. They can make one under Settings, People. If you are the
            only admin, run <code>quill-users reset-link</code> on the server.
          </p>
        )}
      </div>
    </Card>
  );
}

export function Setup({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, submit } = useSubmit(() => api.setup(email, password, name), onDone);
  return (
    <Card title="Set up Quill" sub="Create the first admin account. You can invite your team from Settings afterwards.">
      <form onSubmit={submit} className="auth-form">
        <label className="field">
          <span>Your name</span>
          <input className="input lg" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={80} />
        </label>
        <label className="field">
          <span>Email</span>
          <input className="input lg" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <PasswordField label="Password" value={password} onChange={setPassword} autoComplete="new-password" large />
        <ErrorLine error={error} />
        <button className="btn primary lg" type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create admin account"}
        </button>
      </form>
    </Card>
  );
}

/** Loads a token preview; the card shows why a dead link can't be used. */
function usePreview<T>(token: string | null, load: (t: string) => Promise<T>) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error"; message: string } | { kind: "ok"; data: T }>({ kind: "loading" });
  useEffect(() => {
    if (!token) {
      setState({ kind: "error", message: "This link is missing its code. Copy the whole link and try again." });
      return;
    }
    let live = true;
    load(token).then(
      (data) => live && setState({ kind: "ok", data }),
      (e) => live && setState({ kind: "error", message: errorText(e) }),
    );
    return () => {
      live = false;
    };
  }, [token, load]);
  return state;
}

const DeadLink = ({ title, message }: { title: string; message: string }) => (
  <Card
    title={title}
    sub={message}
    icon={
      <span className="brand-mark warn" aria-hidden>
        <CircleAlert size={22} />
      </span>
    }
  >
    <button className="btn primary lg" type="button" onClick={() => navigate("/", { replace: true })}>
      Go to sign in
    </button>
  </Card>
);

const Loading = () => (
  <div className="auth-wrap" aria-busy="true">
    <div className="auth-card">
      <div className="skeleton" style={{ height: 44, width: 44, borderRadius: 13 }} />
      <div className="skeleton" style={{ height: 22, width: "60%" }} />
      <div className="skeleton" style={{ height: 120 }} />
    </div>
  </div>
);

const loadInvite = (t: string) => api.invitePreview(t);
const loadReset = (t: string) => api.resetPreview(t);

export function Accept({ token, onDone }: { token: string | null; onDone: () => void }) {
  const preview = usePreview<InvitePreview>(token, loadInvite);
  if (preview.kind === "loading") return <Loading />;
  if (preview.kind === "error") return <DeadLink title="This invite can't be used" message={preview.message} />;
  return <AcceptForm token={token!} inv={preview.data} onDone={onDone} />;
}

function AcceptForm({ token, inv, onDone }: { token: string; inv: InvitePreview; onDone: () => void }) {
  const [name, setName] = useState(inv.name);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, submit } = useSubmit(
    async () => {
      await api.accept(token, { password, name, email: inv.email ? undefined : email });
      const first = name.trim().split(" ")[0];
      toast(`Welcome to Quill${first ? `, ${first}` : ""}`);
    },
    () => {
      navigate("/", { replace: true });
      onDone();
    },
  );
  const left = timeLeft(inv.expires_at);
  return (
    <Card
      title="Join Quill"
      sub={
        <>
          <b>{inv.invited_by}</b> invited you to Quill, where your team keeps meeting recordings, transcripts and notes.
        </>
      }
    >
      <div className="auth-chips">
        {inv.is_admin && (
          <span className="pill run">
            <Shield size={11} /> Admin
          </span>
        )}
        {left && (
          <span className="pill">
            <Clock size={11} /> Link expires {left}
          </span>
        )}
      </div>
      <form onSubmit={submit} className="auth-form">
        {inv.email ? (
          <div className="field">
            <span>You'll sign in with</span>
            <div className="auth-as">{inv.email}</div>
          </div>
        ) : (
          <label className="field">
            <span>Email</span>
            <input className="input lg" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          </label>
        )}
        <label className="field">
          <span>Your name</span>
          <input className="input lg" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} autoFocus={!!inv.email} maxLength={80} placeholder="How teammates see you" />
        </label>
        {/* lets password managers save the new login under the right email */}
        {inv.email && <input type="email" autoComplete="username" value={inv.email} readOnly hidden />}
        <PasswordField label="Choose a password" value={password} onChange={setPassword} autoComplete="new-password" large />
        <ErrorLine error={error} />
        <button className="btn primary lg" type="submit" disabled={busy}>
          {busy ? "Creating your account…" : "Create account"}
        </button>
      </form>
      <div className="auth-foot">
        <span className="muted">Already have an account?</span>{" "}
        <button type="button" className="linkish" onClick={() => navigate("/", { replace: true })}>
          Sign in
        </button>
      </div>
    </Card>
  );
}

export function Reset({ token, onDone }: { token: string | null; onDone: () => void }) {
  const preview = usePreview<ResetPreview>(token, loadReset);
  if (preview.kind === "loading") return <Loading />;
  if (preview.kind === "error") return <DeadLink title="This reset link can't be used" message={preview.message} />;
  return <ResetForm token={token!} data={preview.data} onDone={onDone} />;
}

function ResetForm({ token, data, onDone }: { token: string; data: ResetPreview; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const { busy, error, submit } = useSubmit(
    async () => {
      await api.reset(token, password);
      toast("Password saved. Your other devices were signed out.");
    },
    () => {
      navigate("/", { replace: true });
      onDone();
    },
  );
  return (
    <Card
      title="Choose a new password"
      sub={
        data.name ? (
          <>
            For <b>{data.name}</b> · {data.email}
          </>
        ) : (
          <>
            For <b>{data.email}</b>
          </>
        )
      }
    >
      <form onSubmit={submit} className="auth-form">
        <input type="email" autoComplete="username" value={data.email} readOnly hidden />
        <PasswordField label="New password" value={password} onChange={setPassword} autoComplete="new-password" large autoFocus />
        <p className="auth-help">Saving it signs you out on your other devices.</p>
        <ErrorLine error={error} />
        <button className="btn primary lg" type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save password and sign in"}
        </button>
      </form>
    </Card>
  );
}

/** An invite or reset link opened while already signed in (often an admin testing their own link). */
export function SignedInLink({ user, kind, onSignOut }: { user: User; kind: "invite" | "reset"; onSignOut: () => void }) {
  return (
    <Card
      title="You're already signed in"
      sub={
        <>
          As <b>{displayName(user)}</b> ({user.email}). To use this {kind === "invite" ? "invite" : "password reset"} link, sign out first. The link stays valid.
        </>
      }
      icon={<PersonAvatar user={user} size={44} />}
    >
      <button className="btn primary lg" type="button" onClick={onSignOut}>
        Sign out and continue
      </button>
      <button className="btn lg" type="button" onClick={() => navigate("/", { replace: true })}>
        Back to Quill
      </button>
    </Card>
  );
}
