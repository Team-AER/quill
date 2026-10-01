import type { User } from "../api/types";
import { personInitials } from "../lib/people";

/** A signed-in person (not a speaker): initials on the brand gradient, muted when the account is off. */
export function PersonAvatar({ user, size = 24, off }: { user: Pick<User, "name" | "email">; size?: number; off?: boolean }) {
  return (
    <span className={`avatar ${off ? "off" : ""}`} style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.4)) }} aria-hidden>
      {personInitials(user)}
    </span>
  );
}
