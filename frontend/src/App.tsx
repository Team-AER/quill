import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api/client";
import type { User } from "./api/types";
import { Shell } from "./components/Shell";
import { Toasts } from "./components/Toasts";
import { navigate, useRoute, type SettingsSection } from "./lib/router";
import { Accept, Login, Reset, Setup, SignedInLink } from "./screens/Auth";
import { LibraryScreen } from "./screens/Library";
import { MeetingScreen } from "./screens/Meeting";
import { SearchScreen } from "./screens/Search";
import { SettingsScreen } from "./screens/Settings";

type Auth =
  | { kind: "loading" }
  | { kind: "setup" }
  | { kind: "anon"; notice?: string }
  | { kind: "user"; user: User }
  | { kind: "error"; message: string };

export function App() {
  const route = useRoute();
  const [auth, setAuth] = useState<Auth>({ kind: "loading" });

  const check = useCallback(async () => {
    try {
      const st = await api.authState();
      if (st.needs_setup) return setAuth({ kind: "setup" });
      const user = await api.me();
      setAuth({ kind: "user", user });
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) setAuth({ kind: "anon" });
      else setAuth({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  useEffect(() => {
    void check();
    // A 401 mid-session: signed out elsewhere, password changed, or the account was turned off.
    const onUnauth = () => setAuth((a) => (a.kind === "user" ? { kind: "anon", notice: "You were signed out. Sign in again to continue." } : a));
    window.addEventListener("quill:unauthorized", onUnauth);
    return () => window.removeEventListener("quill:unauthorized", onUnauth);
  }, [check]);

  // Keep the URL honest for auth-only routes (invite and reset links say so themselves).
  useEffect(() => {
    if (auth.kind === "user" && (route.name === "login" || route.name === "setup")) {
      navigate(route.params.get("next") || "/", { replace: true });
    }
  }, [auth.kind, route.name, route.params]);

  const done = () => void check();
  const signOut = async () => {
    await api.logout().catch(() => {});
    setAuth({ kind: "anon" });
  };

  let body;
  if (auth.kind === "loading") body = <div className="auth-wrap" aria-busy="true" />;
  else if (auth.kind === "error")
    body = (
      <div className="auth-wrap">
        <div className="auth-card">
          <h1>Can't reach Quill</h1>
          <p>{auth.message}</p>
          <button className="btn primary" onClick={() => void check()}>
            Try again
          </button>
        </div>
      </div>
    );
  else if ((route.name === "accept" || route.name === "reset") && auth.kind === "user")
    body = <SignedInLink user={auth.user} kind={route.name === "accept" ? "invite" : "reset"} onSignOut={() => void signOut()} />;
  else if (route.name === "accept") body = <Accept token={route.params.get("token")} onDone={done} />;
  else if (route.name === "reset") body = <Reset token={route.params.get("token")} onDone={done} />;
  else if (auth.kind === "setup") body = <Setup onDone={done} />;
  else if (auth.kind === "anon") body = <Login onDone={done} notice={auth.notice} />;
  else {
    const user = auth.user;
    let screen;
    switch (route.name) {
      case "meeting":
        screen = <MeetingScreen key={route.id} id={route.id!} user={user} initialT={Number(route.params.get("t")) || null} />;
        break;
      case "search":
        screen = <SearchScreen initialQ={route.params.get("q") ?? ""} />;
        break;
      case "settings":
        screen = <SettingsScreen user={user} section={(route.id ?? "account") as SettingsSection} onUser={(u) => setAuth({ kind: "user", user: u })} />;
        break;
      case "library":
        screen = <LibraryScreen />;
        break;
      default:
        screen = (
          <div className="page">
            <div className="page-inner empty">
              <h3>Page not found</h3>
              <a href="/">Back to the library</a>
            </div>
          </div>
        );
    }
    body = (
      <Shell
        user={user}
        route={route}
        onUser={(u) => setAuth({ kind: "user", user: u })}
        onLogout={async () => {
          await signOut();
          navigate("/", { replace: true });
        }}
      >
        {screen}
      </Shell>
    );
  }
  return (
    <>
      {body}
      <Toasts />
    </>
  );
}
