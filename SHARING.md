# Sharing a project (V1 sync)

Every teammate runs the full Playground on their own computer, with their own copy of
the project. The copies meet at the project's **home**. Code: `luma_playground/core/sync.py`. UI:
`app/sheets/people.py` and `app/sync_ui.py`. Tests: `tests/test_sync_*.py`.

## Where a project lives

| Home | For | Honest about |
|---|---|---|
| **Development server** (the main path) | "Our dev box in the closet." Always on, so anyone syncs any time. It can also run previews. | Needs a one-time setup on the server: with sudo (recommended; taking someone off works at once for everything), or without sudo (People says plainly what that can't promise). |
| **This computer** | Small teams with no server. The owner's own sshd serves the project. | "Teammates can only work on this project while this computer is on." Shown to the owner in People. Teammates see "Sam's computer is off or out of reach. Your changes are safe here." |
| *A folder* | Tests and advanced use: a bare repository on a shared drive. | No access control beyond file permissions, so the UI never offers it. |

On the owner's computer, teammates are reached on the LAN address, a Tailscale address (100.64/10)
or `name.local`, whichever answers first. There is no relay. The Luma Hub can carry invites and
welcomes later; nothing depends on it.

## What travels, and how

One bare git repository on the home (`<home>/repo.git`), reached over SSH:

| Ref | What | Who may change it |
|---|---|---|
| `refs/heads/main`, `refs/heads/goal/<goal-id>` | The code. Goal work happens on the goal's branch and is merged into main on release. Never force-pushed. | Owners and builders |
| `refs/playground/state` | Everything in `.playground/` except `local/` and `presence/`: goals, threads, decisions, issues, knowledge, activity, stack, usage, capacity, proof and the vault (ciphertext). Its own history, apart from the code, so switching branches never rewinds a goal. | Owners, builders, reviewers |
| `refs/playground/presence/<member>` | One small JSON blob per person (who, which goal, when). Replaced on each heartbeat; nothing points at old ones, so git's gc throws them away. Out of the records' history. | Only that person |
| `refs/playground/access` | The access list: members, roles, each computer's public key, where the home is, and until when the list counts (72 hours). Signed by the project's own key or by an owner's computer that key trusts (the `issuers` file beside it, signed by the project's own key only). | Only owners, and only properly signed, current and newer |
| `refs/playground/certs` | Fresh passes (certificates) for every member's computer. Public; useless without that computer's private key. | Only owners |
| `refs/tags/playground/restore/<member>/<n>-<device>` | Restore points (see "Restore points" below). Saved once, never moved or deleted. | Owners and builders, each only their own |

In a shared project, `.playground/`, `AGENTS.md` and `.mcp.json` leave the code's history (the last
two hold this computer's own paths, and each computer makes its own). `.playground/.gitignore` keeps
`local/` and `presence/` out of the records; `.playground/.gitattributes` says how they merge (for
anyone using plain git).

On the server there is also `<home>/checkout` (main, updated by a post-receive hook) and
`<home>/stage/<member>` (each builder's preview checkout).

## Sync

`SyncEngine(project_dir)` runs a round every 4 seconds and soon after any change on the bus:

1. Snapshot `.playground/` onto `refs/playground/state` (a stat cache keeps this cheap).
2. Fetch everything from the home in one connection.
3. Merge the records in Python, file type by file type (below).
4. Apply the result to the folder under the store's lock, re-checking every file against the
   snapshot first: anything written in the meantime is merged, never overwritten. New log lines are
   only ever appended, so the watcher sees them as new entries.
5. Push the records, this person's presence, goal branches that moved forward and this person's new
   restore points. If someone pushed first, fetch and merge again. On an owner's computer a newer access
   list and fresh passes go out first, on their own.
6. Code: fast-forward branches that teammates moved (the checked-out one only when it's clean).
   Diverged code is reported in plain words, never merged silently.

Status (`status()` and the `sync.state` event): "Up to date", "Syncing", "Offline, will catch up",
"Waiting to be let in", "Waiting for an owner" (the access list ran out), "You're no longer on this
project", "Sync stopped" (with why), or "Needs a hand" with a plain explanation (for example "Teammates
and you both changed main. Ask Ari to bring the two together"). It also carries `pass_expires`,
`access_expires`, `renew_due` and `renew_words` (and `warning` says the words when renewal is due).
`presence.changed` carries everyone's heartbeat.

### Merges never lose an entry

- `*.jsonl` (activity, threads, usage, analytics): every line from everyone, each once.
- `decisions.md`: line merge; when both added at the same place, both blocks are kept.
- Goals, issues, knowledge, brief: front matter field by field, then section by section, then entry
  by entry. A story stays the same story when it's ticked, so a tick on one side and a new story on
  the other both survive. Where two people changed the same thing, the newer `updated` wins; any
  entry only the other side added is kept.
- TOML and JSON: key by key; tables in arrays by `id` (or `name`). Removing a member wins over an
  edit; anything else changed on one side and removed on the other is kept.
- `vault.json`: the longer valid roster wins and each secret keeps its newest version. Two owners
  changing the roster at once is reported ("Needs a hand"). An owner's Playground re-locks secrets
  for the current roster after such a merge.
- Anything else both changed: both copies are kept (`name.other-<hash>.ext`).

Merges are deterministic: the same inputs give the same result whichever side merges.

## Who gets in: no shared keys, no passwords

- Each computer has its own ed25519 key (`core.security`), made on it and never copied. The computer
  that shared the project also holds the **project's own key** (the project CA).
- **Sync pass**: a certificate with principal `pg-<project>-sync`, no pty, no forwarding, and a forced
  command `<gate> <member> <device fingerprint>`. It lasts 7 days. The gate checks the signed access list
  on every connection, then runs `git upload-pack` or `git receive-pack` for this one repository and
  nothing else. Its pre-receive hook enforces roles: viewers push only their presence, reviewers can't
  touch code, nobody rewrites main, only owners change the access list, restore points are add-only.
- **Development pass**: owners and builders also get core.security's 12-hour certificate (a shell on the
  Dev server, for previews). On the owner's own computer only the sync principal is trusted: teammates
  never get a shell there.

### The signed access list, and why removal works at once

The access list says which computer (by public key) belongs to which member, with which role. It is
signed, and it **expires**: 72 hours after it's signed (`ACCESS_VALID_S`). Every owner's Playground signs
a fresh one whenever less than 48 hours are left (`ACCESS_REFRESH_S`), so while any owner's Playground is
open now and then, nobody notices. The home refuses a list whose time is up (and one claiming more than 96
hours); while it's out of date, only owners may come in, and only to send a fresh one, which their
Playground does first thing. So an old list that someone puts back stops working by itself.

**With the root setup** (`home_setup_instructions(mode="server", trust="live")`, "With sudo" in People),
one script, run once with sudo, installs everything that decides who gets in, owned by root, so the server
account (and anyone with a shell as it) can't change any of it:

- `/etc/ssh/playground/bin/keys`: sshd's `AuthorizedKeysCommand`, run at **every** sign-in. It reads the
  project's access list (as the server account, through `runuser`), checks the signatures itself against
  the root-owned `projects/<id>/ca.pub`, and only then tells sshd which passes count: a sync pass only for
  the project's own gate and that very computer's key, and a shell only for a builder's or owner's computer
  on the current list. Someone taken off gets no shell from the moment the new list reaches the server,
  even though their pass is still in date.
- `projects/<id>/gate` and `projects/<id>/hooks/`: the gate and the role checks. Pushes through the gate
  run these hooks (`core.hooksPath`), and so do pushes from a shell.
- `projects/<id>/live`: which account and where the repository is; `revoked.krl` as a second lock.
- A drop-in, `sshd_config.d/41-playground-live.conf`. It refuses to replace another
  `AuthorizedKeysCommand`, and runs `sshd -t` before reloading.

You never need to run it again after taking someone off. Before the first access list exists, only the
computer that shared the project (named in `live`) gets in, so it can send one.

**Without sudo** (`trust="account"`, "Without sudo" in People): the project's key goes into the server
account's own `~/.ssh/authorized_keys`, limited to the project's passes. Taking someone off still stops
their **syncing** at once (the gate reads the list every time). But nothing can check a **shell** at every
sign-in without root, so a removed builder's or owner's development pass keeps working until it runs out,
within 12 hours; and while they had a shell they could have changed the account's own settings. People
says this in those words before you choose it, on the "Where it lives" row, and when you remove someone.

**On the owner's computer** ("This computer"): teammates only ever get the sync principal, so the gate is
the whole story and removal is immediate.

### Any owner can renew

- Owners' computers other than the one that shared the project sign with their own device key. The
  project's own key lists them in the `issuers` file (it updates that list whenever someone becomes or
  stops being an owner). The home and every member check the chain: project key → issuers → access list.
- Such a computer re-signs the access list and renews everyone's passes (sync: when 3 days are left;
  development: 4 hours), exactly like the first. With the root setup the home accepts passes they sign;
  without it (the account's own `authorized_keys` names only the project's key) only the computer that
  shared the project hands out passes, and People says so.
- A computer that stops being an owner's stays in `issuers` as `retired` for 7 days: passes it signed keep
  working until fresh ones arrive, and it can't sign a list. A pass only ever works for a computer on the
  current list, with that list's role, so no signer can let anyone in by itself.
- Changes an owner makes (let in, take off, change a role, take a computer off) are kept as pending steps
  in `<data>/sync/<project>/pending.json` and applied on top of the newest list until the home has a list
  that grew from one of theirs, so two owners changing things at once both land.
- Invites and welcomes still come from the computer that shared the project (they carry the project's own
  key for the safety code).

### When nobody renews: warnings, and asking for a fresh pass

- A member's Playground says, two days ahead (`PASS_WARN_S`): "Your pass to the project home runs out in
  2 days. It renews by itself when an owner's Playground is open. If nobody will be around, ask an owner for a
  fresh pass in People." The sync dot turns amber, and the access list running out within a day is said
  the same way.
- **Ask for a fresh pass** (People): a signed request (`PGR1-…`), the same kind of code as asking to join:
  this computer's public key, signed by it, nothing secret. Any owner's computer that can hand out passes
  pastes it under "Let someone in" (it must be a computer already on the list), and answers with fresh
  passes (`PGP1-…`), signed by that owner's computer. The member's Playground checks the signer is the
  project's key or one it trusts, keeps the passes, and syncs again.

### Removing someone, or one of their computers

- **Take someone off** (any owner's computer): a new list without them. From the moment it reaches the
  home their sync is refused ("An owner took this computer off the project"), and with the root setup
  every shell too. On the computer that shared the project their keys also go into the revocation list.
  The vault is re-encrypted without them, and People names the secrets to change where they come from.
- **Take a computer off** (a lost laptop): only that computer's key leaves the list; the person keeps
  working on their others. Its vault key goes too (People records which vault key belongs to which
  computer when it lets one in).
- **Moving the home** is a newer signed access list. Each device keeps what it trusts (project key, home,
  host key, the owner computers it trusts) in `<data>/sync/<project>/pin.json`, never in the synced
  folder, so a teammate who edits `project.toml` can't redirect anyone.
- Host keys are pinned from the invite (`HostKeyAlias`, a per-project `known_hosts`, strict
  checking). ssh runs with `-F none`: no `~/.ssh/config`, no agent, no other keys.

## Joining

All files and copyable codes (`PGI1-…`, `PGJ1-…`, `PGA1-…`, a few hundred characters). None holds a
secret. Each is signed with `ssh-keygen -Y sign`.

1. **Invite** (owner, People › Invite): the project id and name, the home (hosts, port, account,
   folder, host key), the CA public key, the role, and an expiry. Signed by the CA. Shows a twelve-digit
   **safety code** derived from the CA key.
2. **Ask to join** (invitee, People › Join a project): checks the signature and expiry, shows the
   same safety code ("read the numbers to each other"), and answers with this device's SSH public key
   and vault public key, signed by the device key.
3. **Let them in** (owner): checks the signature and that it answers one of *our* invites (each works
   once). Approving registers the device with the CA, adds it to the vault, marks them joined in
   `project.toml`, re-signs the access list, issues their passes and pushes. The **welcome** carries
   their first passes and the home, signed by the CA.
4. **Fetch** (invitee): checks the welcome against the CA pinned from the invite, keeps the passes,
   clones the code, opens the records, writes this computer's own `AGENTS.md` and `.mcp.json`, and
   syncs from then on.

**A second computer for the same person** is another invite for the same member (People › the person's
menu › "Invite another of their computers", or "Another computer of yours"). That computer makes its own
key and joins as the same member with the same role: its own seat on the access list, its own passes, its
own vault key, one person in People ("On 2 computers"). Their two computers share one presence note; when
one quits while the other is open, it doesn't say "left".

## Agents, models and previews

Agents run on the prompting person's own computer against their own clone, with their own model or
account. Sync carries files, never prompts or credentials; no one's prompt runs on someone else's
plan. Previews can run on the Dev server: `SyncEngine.stage_place()` pushes the current branch,
updates `<home>/stage/<member>` over the development pass, and returns a place for
`core.preview.start_preview(place=…)`, which tunnels the port back.

## Restore points

In a shared project a restore point is the tag `playground/restore/<member>/<n>-<device>`
(`places.save_restore_point`, through `sync.restore_tag_name`): who saved it and on which computer, so two
people (or one person's two computers) saving at the same moment never clash. `n` is still the next number
after everything this computer knows of, so History reads "Restore point 15" as before; activity records
the full id as `restore_id`. Sync sends your own restore points to the home and brings teammates' back as
tags (never moving one that's already here); the home accepts them add-only, each person only their own.
So History shows everyone's restore points, and anyone with the code can go back to one.
`places.rollback`, `rollback_plan` and `authorize_rollback` take the id or the number.

## Known limits

- **Someone with a shell on the server can put an old access list back.** It works only until its time
  is up (at most 72 hours after it was signed), and any owner's Playground that's open puts the newer one
  back within seconds. With the root setup, they can't change the checks themselves. Anything they left
  running as the server account while they had a shell is theirs: if you remove someone you don't trust,
  look at that account (its `~/.ssh`, crontab) yourself.
- **Without sudo**, a removed builder's or owner's shell works until their pass runs out (12 hours), and
  only the computer that shared the project hands out passes.
- **The computer that shared the project holds the project's own key.** Another owner can't take that
  person off (People explains); and if that computer is lost, the project's key goes with it: the other
  owners keep everything running, but nobody can let new people in until the project is shared again from
  another owner's computer.
- **If no owner's Playground is open for 3 days** the access list runs out and teammates wait ("Waiting
  for an owner"). Their copies keep working; the first owner to open Playground fixes it.
- Making someone an owner doesn't make them a vault admin: they can read and write secrets, but changing
  who can open the vault stays with the owners who already could.
- The release lock is per computer: two teammates releasing at the same moment both release (their restore
  points don't clash, but the second release replaces the first on production).
- Old commits from before sharing still contain `.playground/`. Checking one out by hand overwrites
  the records in the folder, and the next sync records that as an edit. Don't restore old commits with
  `git checkout <old> -- .`; Playground's own restore goes through a goal.
- Presence of someone who was removed stays on the home until an owner deletes the ref; People shows
  only current members, so nobody sees it.
- The home must have OpenSSH 8.1 or newer (`ssh-keygen -Y verify`) and a POSIX shell; the root setup also
  needs `runuser` (util-linux) and an sshd that reads `sshd_config.d`.
