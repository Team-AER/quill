import { useSyncExternalStore } from "react";

type Listener = () => void;
const listeners = new Set<Listener>();

function emit() {
  listeners.forEach((l) => l());
}
window.addEventListener("popstate", emit);

export function navigate(to: string, opts: { replace?: boolean } = {}) {
  if (to === location.pathname + location.search) return;
  if (opts.replace) history.replaceState(null, "", to);
  else history.pushState(null, "", to);
  emit();
  if (!opts.replace) window.scrollTo(0, 0);
}

function snapshot() {
  return location.pathname + location.search;
}

export interface Route {
  path: string;
  params: URLSearchParams;
  name: "library" | "meeting" | "search" | "settings" | "login" | "setup" | "accept" | "reset" | "notfound";
  /** meeting id, or the settings section */
  id?: string;
}

export type SettingsSection = "account" | "people" | "system";
const SECTIONS: SettingsSection[] = ["account", "people", "system"];

export function parseRoute(href: string): Route {
  const u = new URL(href, "http://x");
  const path = u.pathname.replace(/\/+$/, "") || "/";
  const params = u.searchParams;
  if (path === "/") return { path, params, name: "library" };
  if (path === "/search") return { path, params, name: "search" };
  if (path === "/settings") return { path, params, name: "settings", id: "account" };
  if (path === "/login") return { path, params, name: "login" };
  if (path === "/setup") return { path, params, name: "setup" };
  if (path === "/accept") return { path, params, name: "accept" };
  if (path === "/reset") return { path, params, name: "reset" };
  let m = /^\/settings\/([^/]+)$/.exec(path);
  if (m) return SECTIONS.includes(m[1] as SettingsSection) ? { path, params, name: "settings", id: m[1] } : { path, params, name: "notfound" };
  // Token links: /invite/<token> and /reset/<token> (the token rides in params like ?token=)
  m = /^\/(invite|reset)\/([^/]+)$/.exec(path);
  if (m) {
    const p = new URLSearchParams(params);
    p.set("token", decodeURIComponent(m[2]));
    return { path, params: p, name: m[1] === "invite" ? "accept" : "reset" };
  }
  m = /^\/m\/([^/]+)$/.exec(path);
  if (m) return { path, params, name: "meeting", id: decodeURIComponent(m[1]) };
  return { path, params, name: "notfound" };
}

export function useRoute(): Route {
  const href = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    snapshot,
  );
  return parseRoute(href);
}

/** Intercepts plain left clicks on internal <a> so the app stays SPA. */
export function linkClick(e: React.MouseEvent<HTMLAnchorElement>) {
  const a = e.currentTarget;
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (a.target && a.target !== "_self") return;
  if (a.hasAttribute("download")) return;
  const url = new URL(a.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.preventDefault();
  navigate(url.pathname + url.search);
}
