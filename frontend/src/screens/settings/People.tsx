import { Ban, KeyRound, Link2, LogOut, MoreHorizontal, RefreshCw, Shield, ShieldOff, Trash2, UserCheck, UserPlus, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Invite, Person, User } from "../../api/types";
import { Confirm } from "../../components/Confirm";
import { InviteSheet, LinkSheet } from "../../components/InviteSheet";
import { Menu } from "../../components/Menu";
import { PersonAvatar } from "../../components/PersonAvatar";
import { displayName, inline, inviteMessage, relativeTime, resetMessage, timeLeft } from "../../lib/people";
import { navigate, useRoute } from "../../lib/router";
import { formatBytes, formatDate } from "../../lib/time";
import { errorText, toast } from "../../lib/toast";

type LinkData = Parameters<typeof LinkSheet>[0]["data"];
type Ask = { title: string; body: string; label: string; danger?: boolean; run: () => Promise<void> };

/** Admin: everyone on this Quill, and the invite links still waiting to be used. */
export function PeopleSection({ user, onSelfChanged, reloadKey }: { user: User; onSelfChanged: () => void; reloadKey: number }) {
  const route = useRoute();
  const [people, setPeople] = useState<Person[] | null>(null);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [inviting, setInviting] = useState(false);
  const [link, setLink] = useState<LinkData>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [removing, setRemoving] = useState<Person | null>(null);
  const me = displayName(user);

  const load = useCallback(async () => {
    try {
      const [p, i] = await Promise.all([api.people(), api.invites()]);
      setPeople(p);
      setInvites(i);
    } catch (e) {
      toast(errorText(e), "error");
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // ⌘K "Invite someone" lands here with ?invite=1
  useEffect(() => {
    if (route.params.get("invite")) {
      setInviting(true);
      navigate("/settings/people", { replace: true });
    }
  }, [route.params]);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    try {
      await fn();
      if (done) toast(done);
      await load();
    } catch (e) {
      toast(errorText(e), "error");
    }
  };

  const setRole = (p: Person, admin: boolean) => {
    const self = p.id === user.id;
    const run = () =>
      act(async () => {
        await api.updatePerson(p.id, { is_admin: admin });
        if (self) onSelfChanged();
      }, `${self ? "You are" : `${displayName(p)} is`} now ${admin ? "an admin" : "a member"}`);
    if (admin)
      setAsk({
        title: `Make ${displayName(p)} an admin?`,
        body: "Admins can invite and remove people, reset passwords and change system settings. They still only see their own meetings.",
        label: "Make admin",
        run,
      });
    else if (self)
      setAsk({ title: "Step down as admin?", body: "You'll lose access to People and System settings. Another admin can make you an admin again.", label: "Step down", danger: true, run });
    else void run();
  };

  const setDisabled = (p: Person, disabled: boolean) => {
    const run = () => act(() => api.updatePerson(p.id, { disabled }), disabled ? `${displayName(p)} is turned off` : `${displayName(p)} can sign in again`);
    if (!disabled) return void run();
    setAsk({
      title: `Turn off ${displayName(p)}'s account?`,
      body: `They're signed out right away and can't sign in. ${p.meeting_count ? `Their ${p.meeting_count} meeting${p.meeting_count === 1 ? " is" : "s are"} kept. ` : ""}You can turn the account back on any time.`,
      label: "Turn off",
      danger: true,
      run,
    });
  };

  const resetLink = async (p: Person) => {
    try {
      const r = await api.resetLink(p.id);
      const l = { url: r.url, expires_at: r.expires_at, email: r.email, name: r.name };
      setLink({ title: "Password reset link", sub: `For ${displayName(p)} · works for 24 hours`, link: l, message: resetMessage(l) });
    } catch (e) {
      toast(errorText(e), "error");
    }
  };

  const renew = async (inv: Invite) => {
    try {
      const r = await api.renewInvite(inv.id);
      const l = { url: r.url, expires_at: r.expires_at, email: r.email, name: r.name };
      setLink({ title: "New invite link", sub: `${r.email ?? "Open link"} · the old link no longer works`, link: l, message: inviteMessage(l, me) });
      await load();
    } catch (e) {
      toast(errorText(e), "error");
    }
  };

  const waiting = invites.filter((i) => i.status === "pending" || i.status === "expired");
  const admins = people?.filter((p) => p.is_admin && !p.disabled).length ?? 0;

  return (
    <>
      <div className="section-head">
        <div>
          <h2>People</h2>
          <p className="sub">
            {people ? `${people.length} ${people.length === 1 ? "person" : "people"} · ${admins} admin${admins === 1 ? "" : "s"}` : "Loading…"} · Everyone's meetings are private
            to them.
          </p>
        </div>
        <button type="button" className="btn primary" onClick={() => setInviting(true)}>
          <UserPlus size={14} /> Invite someone
        </button>
      </div>

      {waiting.length > 0 && (
        <section className="cardsheet" aria-labelledby="invites-h">
          <h2 id="invites-h">Waiting to join</h2>
          <p className="sub">Invite links that haven't been used yet. Lost a link? Make a new one; the old one stops working.</p>
          <ul className="invites">
            {waiting.map((inv) => (
              <InviteRow key={inv.id} inv={inv} onRenew={() => void renew(inv)} onWithdraw={() => void act(() => api.withdrawInvite(inv.id), "Invite withdrawn")} />
            ))}
          </ul>
        </section>
      )}

      <section className="cardsheet" aria-label="Everyone">
        {people === null ? (
          <div className="skeleton" style={{ height: 160 }} />
        ) : (
          <ul className="people">
            {people.map((p) => (
              <PersonRow
                key={p.id}
                p={p}
                self={p.id === user.id}
                onRole={(admin) => setRole(p, admin)}
                onDisabled={(off) => setDisabled(p, off)}
                onReset={() => void resetLink(p)}
                onSignOut={() => void act(() => api.signOutPerson(p.id), `Signed ${displayName(p)} out everywhere`)}
                onRemove={() => setRemoving(p)}
              />
            ))}
          </ul>
        )}
        {people?.length === 1 && waiting.length === 0 && (
          <div className="people-empty">
            <p>It's just you so far. Invite your team: Quill gives you a link to send them over chat.</p>
          </div>
        )}
      </section>

      <InviteSheet open={inviting} from={me} onClose={() => setInviting(false)} onCreated={() => void load()} />
      <LinkSheet data={link} onClose={() => setLink(null)} />
      <Confirm
        open={!!ask}
        title={ask?.title ?? ""}
        body={ask?.body ?? ""}
        confirmLabel={ask?.label ?? "OK"}
        danger={ask?.danger}
        onCancel={() => setAsk(null)}
        onConfirm={() => {
          const a = ask;
          setAsk(null);
          void a?.run();
        }}
      />
      <RemoveDialog
        person={removing}
        me={me}
        onCancel={() => setRemoving(null)}
        onRemove={(p, meetings) => {
          setRemoving(null);
          void act(
            () => api.removePerson(p.id, meetings),
            `Removed ${displayName(p)}${p.meeting_count ? (meetings === "transfer" ? `; their meetings are now yours` : `; their meetings were deleted`) : ""}`,
          );
        }}
      />
    </>
  );
}

function InviteRow({ inv, onRenew, onWithdraw }: { inv: Invite; onRenew: () => void; onWithdraw: () => void }) {
  const expired = inv.status === "expired";
  return (
    <li className={expired ? "expired" : undefined}>
      <span className="inv-ico" aria-hidden>
        <Link2 size={15} />
      </span>
      <div className="inv-main">
        <b>
          {inv.email ?? "Open link"}
          {inv.name && <span className="muted"> · {inv.name}</span>}
          {inv.is_admin && (
            <span className="pill run">
              <Shield size={10} /> Admin
            </span>
          )}
        </b>
        <span>
          {expired ? `Expired ${inline(formatDate(inv.expires_at))}` : `Expires ${timeLeft(inv.expires_at)}`}
          {inv.invited_by ? ` · from ${inv.invited_by}` : ""}
          {inv.created_at ? `, ${inline(relativeTime(inv.created_at))}` : ""}
        </span>
      </div>
      <button type="button" className="btn sm" onClick={onRenew} title="Make a fresh link. The old one stops working.">
        <RefreshCw size={12} /> New link
      </button>
      <button type="button" className="icon-btn sm" onClick={onWithdraw} aria-label={`Withdraw invite for ${inv.email ?? "open link"}`} title="Withdraw">
        <X size={14} />
      </button>
    </li>
  );
}

interface RowProps {
  p: Person;
  self: boolean;
  onRole: (admin: boolean) => void;
  onDisabled: (off: boolean) => void;
  onReset: () => void;
  onSignOut: () => void;
  onRemove: () => void;
}

function PersonRow({ p, self, onRole, onDisabled, onReset, onSignOut, onRemove }: RowProps) {
  const activity = p.disabled
    ? `Turned off ${inline(formatDate(p.disabled_at))}`
    : self
      ? "Active now"
      : p.last_seen_at
        ? `Active ${inline(relativeTime(p.last_seen_at))}`
        : "Hasn't signed in";
  return (
    <li className={p.disabled ? "off" : undefined}>
      <PersonAvatar user={p} size={34} off={p.disabled} />
      <div className="p-main">
        <b>
          {displayName(p)}
          {self && <span className="you">You</span>}
        </b>
        <span title={p.email}>{p.email}</span>
      </div>
      <div className="p-role">
        {p.disabled ? (
          <span className="pill failed">
            <Ban size={10} /> Turned off
          </span>
        ) : p.is_admin ? (
          <span className="pill run">
            <Shield size={10} /> Admin
          </span>
        ) : (
          <span className="pill">Member</span>
        )}
      </div>
      <div className="p-meta">
        <span>{activity}</span>
        <span className="muted">
          {p.meeting_count} meeting{p.meeting_count === 1 ? "" : "s"}
          {p.uploaded_bytes ? ` · ${formatBytes(p.uploaded_bytes)}` : ""}
        </span>
      </div>
      <Menu label={`Manage ${displayName(p)}`} button={<MoreHorizontal size={16} />}>
        {(close) => (
          <>
            {!p.disabled &&
              (p.is_admin ? (
                <button type="button" onClick={() => (close(), onRole(false))}>
                  <ShieldOff size={14} /> {self ? "Step down to member" : "Make member"}
                </button>
              ) : (
                <button type="button" onClick={() => (close(), onRole(true))}>
                  <Shield size={14} /> Make admin
                </button>
              ))}
            {self ? (
              <a href="/settings" onClick={(e) => (e.preventDefault(), close(), navigate("/settings"))}>
                <KeyRound size={14} /> Change your password
              </a>
            ) : (
              <>
                {!p.disabled && (
                  <button type="button" onClick={() => (close(), onReset())}>
                    <KeyRound size={14} /> Password reset link
                  </button>
                )}
                <button type="button" onClick={() => (close(), onSignOut())} disabled={!p.session_count}>
                  <LogOut size={14} /> Sign out everywhere
                </button>
                <div className="sep" />
                {p.disabled ? (
                  <button type="button" onClick={() => (close(), onDisabled(false))}>
                    <UserCheck size={14} /> Turn account on
                  </button>
                ) : (
                  <button type="button" onClick={() => (close(), onDisabled(true))}>
                    <Ban size={14} /> Turn account off
                  </button>
                )}
                <button type="button" className="danger" onClick={() => (close(), onRemove())}>
                  <Trash2 size={14} /> Remove…
                </button>
              </>
            )}
          </>
        )}
      </Menu>
    </li>
  );
}

function RemoveDialog({ person, me, onCancel, onRemove }: { person: Person | null; me: string; onCancel: () => void; onRemove: (p: Person, meetings: "transfer" | "delete") => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [choice, setChoice] = useState<"transfer" | "delete">("transfer");
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (person && !d.open) {
      setChoice("transfer");
      d.showModal();
    }
    if (!person && d.open) d.close();
  }, [person]);
  const n = person?.meeting_count ?? 0;
  return (
    <dialog ref={ref} className="confirm remove-dialog" onCancel={(e) => (e.preventDefault(), onCancel())} aria-labelledby="rm-title">
      {person && (
        <>
          <h2 id="rm-title">Remove {displayName(person)}?</h2>
          <p>
            {person.email} won't be able to sign in, and the account can't be brought back. To keep it and only block sign-in, turn the account off
            instead.
          </p>
          {n > 0 && (
            <fieldset className="rm-choice">
              <legend>
                Their {n} meeting{n === 1 ? "" : "s"}
              </legend>
              <label>
                <input type="radio" name="rm" checked={choice === "transfer"} onChange={() => setChoice("transfer")} />
                <span>
                  <b>Move to {me}</b>
                  <span>They appear in your library.</span>
                </span>
              </label>
              <label>
                <input type="radio" name="rm" checked={choice === "delete"} onChange={() => setChoice("delete")} />
                <span>
                  <b>Delete permanently</b>
                  <span>Recordings, transcripts and notes are erased.</span>
                </span>
              </label>
            </fieldset>
          )}
          <div className="actions">
            <button type="button" className="btn" onClick={onCancel} autoFocus>
              Cancel
            </button>
            <button type="button" className="btn danger" onClick={() => onRemove(person, choice)}>
              {n > 0 && choice === "delete" ? `Remove and delete ${n} meeting${n === 1 ? "" : "s"}` : `Remove ${displayName(person)}`}
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}
