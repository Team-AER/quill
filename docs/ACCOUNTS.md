# Accounts: admins, members and invite links

Plan and reference for Quill's user model on a small self-hosted install (one team, a
handful to a few dozen people, no mail server). Written 2026-10-01.

## Where we started

Quill already had first-run admin setup (LAN only), email + password sign-in with scrypt,
30-day HttpOnly sessions, rate limits, and admin-created one-time invite links. Gaps:

- The invite page never said who invited you or which email you were joining as.
- An invite link was shown once. If it was lost, there was no way to get a new one or withdraw it.
- Nobody could change their own password, name or email, or see where they were signed in.
- An admin could not see the people on the install, change roles, turn an account off,
  help someone who forgot their password, or remove anyone.
- A lone admin who forgot their password needed hand-written SQL.

## Principles

1. **Links instead of email.** Every flow that would normally send a mail (invite,
   password reset) gives the admin a link and a ready-to-send message to paste into chat.
   Links are single-use and expire. Only a keyed hash of each token is stored, so a lost link
   gets **replaced** with a new one. It is never shown again.
2. **Two roles.** *Admin* manages people and system settings. *Member* uploads and reads
   their own meetings. Meetings stay private to their owner. Sharing is a later phase.
3. **No lockouts.** There is always at least one active admin. The server refuses to
   demote, turn off or remove the last one. There is a shell command for the case where
   that admin forgets their password.
4. **Each change is undoable or confirmed.** Turning an account off keeps its meetings and
   is reversible. Removing someone asks what happens to their meetings.

## Data model (migration 2)

| Table | Change |
| --- | --- |
| `users` | `+ name`, `+ disabled_at`, `+ last_seen_at`, `+ password_changed_at`, `+ invited_by` |
| `sessions` | `+ id` (public id), `+ created_at`, `+ last_seen_at`, `+ user_agent`, `+ ip` |
| `invites` | rebuilt: `id` (public), `token` (digest), `email` **nullable** (empty = anyone with the link may use it once), `name`, `is_admin`, `created_by`, `created_at`, `expires_at`, `used_at`, `used_by`, `revoked_at` |
| `password_resets` | new: `token` digest, `user_id`, `created_by`, `created_at`, `expires_at`, `used_at` |

`last_seen_at` is written at most once every 5 minutes per session, not on every request.

## API

Public (no session):

| Route | Purpose |
| --- | --- |
| `GET /api/auth/invite?token=` | Preview: email (or none), suggested name, role, who invited, expiry. Expired, used and withdrawn links each return their own message |
| `POST /api/auth/accept` | `{token, password, name, email?}`. Email is required only for open links |
| `GET /api/auth/reset?token=` | Preview: the account's email and name |
| `POST /api/auth/reset` | `{token, password}`. Signs out every device, then signs this one in |

Signed in (self):

| Route | Purpose |
| --- | --- |
| `PATCH /api/account` | `{name?, email?, current_password?}`. Changing the email needs the current password |
| `POST /api/account/password` | `{current_password, new_password}`. Signs out other devices |
| `GET /api/account/sessions` | Devices: browser, IP, created, last active, `current` |
| `DELETE /api/account/sessions/{id}` | Sign out one device |
| `POST /api/account/sessions/revoke-others` | Sign out everywhere else |

Admin:

| Route | Purpose |
| --- | --- |
| `GET /api/users` | People with role, status, last active, meeting count, uploaded bytes |
| `PATCH /api/users/{id}` | `{is_admin?, disabled?}` with last-admin and not-yourself guards |
| `POST /api/users/{id}/reset-link` | 24-hour reset link; replaces any earlier unused one |
| `POST /api/users/{id}/sign-out` | End all their sessions |
| `DELETE /api/users/{id}?meetings=transfer\|delete` | Remove; their meetings move to you or are deleted |
| `GET /api/invites` | Pending, expired, used and withdrawn invites |
| `POST /api/invites` | `{email?, name?, is_admin, days}` (1, 7 or 30). Re-inviting an email withdraws its older link |
| `POST /api/invites/{id}/renew` | New link and expiry; the old link stops working |
| `DELETE /api/invites/{id}` | Withdraw |

A turned-off account cannot sign in, and its sessions stop working immediately. Its meetings
are kept.

## UX

**Sign in.** Show/hide password. A "Forgot your password?" link explains that Quill does
not send email and that an admin can make a reset link under Settings, People. A
turned-off account gets a clear message.

**Accept an invite** (`/invite/<token>`). "*Priya invited you to Quill*", showing the email
you're joining as (or an email field for an open link), your name (pre-filled if the admin
typed one), and a password with show/hide and a strength hint. An "Admin" badge appears if
the invite grants it. A dead link explains why (expired on a date, already used, withdrawn)
and who to ask. If you are already signed in, the page offers to sign you out first.

**Reset password** (`/reset/<token>`). The same card: "Choose a new password for
*email*". Afterwards every other device is signed out, and the page says so.

**Settings** is open to everyone now, with sections:
- *Account*: profile (name, email), password, appearance, and devices with "Sign out" per
  row and "Sign out everywhere else".
- *People* (admin): pending invites (copy a new link, withdraw), then everyone on the
  install with role, last active, meeting count and a menu (make admin or member, password
  reset link, sign out everywhere, turn off or on, remove).
- *System* (admin): models, transcription model, source retention (as before).

**Invite sheet.** Email (optional), name (optional), role (Member / Admin), expiry
(1 day / 7 days / 30 days). The result shows the link with **Copy link**, **Copy
message** (a short note with the link and expiry, ready for Slack or WhatsApp), and **Share**
where the browser supports it. The same share panel is used for password reset links.

The rail shows your name and goes to Account. ⌘K gains "Account settings", "Invite
people" and "People".

## Sharing meetings (migration 3)

A meeting belongs to whoever uploaded it. Its owner can share it from the meeting page
(**Share**) with:

- **Teammates**, each with **Can view** (watch, read, search, export) or **Can edit** (also
  rename the meeting and its speakers).
- **Everyone on this Quill**, at either level. A personal share can raise one person above that.

Only the owner can re-run processing, delete the meeting or change sharing. Admins get no
implicit access. Quill sends no notification: the sheet has **Copy link**, and the meeting
appears in the other person's library (filter **Shared with me**) with the owner's name on the
cover. Shares disappear with the meeting or the person. When someone is removed and their
meetings move to you, your own share rows on them are dropped.

| Route | Purpose |
| --- | --- |
| `GET /api/directory` | Active people you can share with (`id`, `name`, `email`) |
| `GET /api/meetings/{id}/sharing` | Owner: `{owner, everyone, people[]}` |
| `PATCH /api/meetings/{id}/sharing` | Owner: `{everyone?: 'view'\|'edit'\|null, people?: {"<user id>": 'view'\|'edit'\|null}}` |

Every meeting response carries `access` (`owner`, `edit` or `view`), `owner` and
`everyone_access`. The owner's responses also carry `shared_with`. The access check lives in
`api.get_meeting(..., need=)` and `sharing.py`. Listing, search, frames, media, exports and live
updates all use the same visibility rule.

People without a display name get a one-line prompt above every page until they add one or
dismiss it, because shared meetings show the owner's name.

## Recovery from a shell

`quill-users` (installed to `/usr/local/bin` in the CT) runs as the `quill` user with the
service environment. `pct exec` has no `/usr/local/bin` on its PATH, so from the Proxmox host use
`ssh root@<proxmox-host> "pct exec <CTID> -- /usr/local/bin/quill-users list"`.

```sh
quill-users list
quill-users reset-link you@example.com   # prints a 24-hour reset link
quill-users set-role you@example.com admin
quill-users enable you@example.com
```

## Later

- Outgoing email (SMTP) for invites and resets, as an optional add-on to links.
- Passkeys or TOTP for admins. SSO through an existing identity provider.
- Persistent rate limits (they are in memory today and reset on restart).
