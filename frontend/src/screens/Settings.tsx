import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import type { User } from "../api/types";
import { linkClick, navigate, type SettingsSection } from "../lib/router";
import { AccountSection } from "./settings/Account";
import { PeopleSection } from "./settings/People";
import { SystemSection } from "./settings/System";

const TABS: { id: SettingsSection; label: string; href: string; admin: boolean }[] = [
  { id: "account", label: "Account", href: "/settings", admin: false },
  { id: "people", label: "People", href: "/settings/people", admin: true },
  { id: "system", label: "System", href: "/settings/system", admin: true },
];

export function SettingsScreen({ user, section, onUser }: { user: User; section: SettingsSection; onUser: (u: User) => void }) {
  const [reloadKey, setReloadKey] = useState(0);
  const tabs = TABS.filter((t) => user.is_admin || !t.admin);
  const current = tabs.some((t) => t.id === section) ? section : "account";
  const signedOut = () => window.dispatchEvent(new CustomEvent("quill:unauthorized"));
  const refreshSelf = async () => {
    try {
      const u = await api.me();
      onUser(u);
      if (!u.is_admin) navigate("/settings", { replace: true });
    } catch {
      /* the 401 handler signs out */
    }
  };

  return (
    <div className="page">
      <div className="page-inner settings-page">
        <div className="page-head">
          <h1>Settings</h1>
          <span className="spacer" />
          <button type="button" className="icon-btn" onClick={() => setReloadKey((k) => k + 1)} aria-label="Refresh" title="Refresh">
            <RefreshCw size={16} />
          </button>
        </div>
        {tabs.length > 1 && (
          <nav className="set-tabs" aria-label="Settings sections">
            {tabs.map((t) => (
              <a key={t.id} href={t.href} onClick={linkClick} aria-current={current === t.id ? "page" : undefined}>
                {t.label}
              </a>
            ))}
          </nav>
        )}
        <div className="set-body" key={current}>
          {current === "account" && <AccountSection user={user} onUser={onUser} onSignedOut={signedOut} reloadKey={reloadKey} />}
          {current === "people" && <PeopleSection user={user} onSelfChanged={() => void refreshSelf()} reloadKey={reloadKey} />}
          {current === "system" && <SystemSection reloadKey={reloadKey} />}
        </div>
      </div>
    </div>
  );
}
