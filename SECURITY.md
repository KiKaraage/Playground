# Playground security design

The aim: no keys passed around by hand, and proper encryption throughout.
This note covers what `core/security.py`, `core/places.py`, `core/preview.py`, `core/checks.py`
and `core/services.py` do about that, and what they deliberately don't do yet.

## Principles

1. **Private keys never move.** Every key is made on the device that uses it, and no API returns
   private key material. Members share *public* keys; the project shares *certificates* and
   *ciphertext*.
2. **Nothing secret in the project folder.** The project syncs between members and through the Hub.
   It holds names, roles, public keys, signatures and ciphertext only. Tests scan every file for
   planted secrets in raw, base64 and hex form.
3. **Standard primitives, standard tools.** OpenSSH (`ssh-keygen`) for keys, certificates and
   KRLs; python3-cryptography for X25519, Ed25519, HKDF-SHA256 and ChaCha20-Poly1305. The only
   composition of our own is envelope encryption in the shape age and HPKE use.
4. **Fail closed, in plain words.** A missing library, a locked keyring, a forged roster or an
   unapproved release stops the action and says why. Nothing falls back to plaintext or prompts.

## Where things live

| What | Where | Mode |
|---|---|---|
| Device SSH key (ed25519) | `<data>/keys/device_ed25519` (+ `.pub`, `device.json`) | dir 0700, key 0600 |
| Device vault keys (X25519 + Ed25519) | `<data>/keys/vault_identity.json` (PKCS#8 PEM) | 0600 |
| Key passphrases | Secret Service via `secret-tool`, when reachable | keyring |
| Project CA (owner only) | `<data>/ca/<project-id>/` | dir 0700, key 0600 |
| Shared project pins, pending changes | `<data>/sync/<project-id>/` (`pin.json`, `pending.json`) | 0600 |
| A home's root-owned checks | `/etc/ssh/playground/bin/keys`, `projects/<id>/{gate,hooks,live,ca.pub}` | root, 0755 |
| Vault trust pins | `<data>/vault-trust/<project-id>.json` | 0600 |
| Vault | `.playground/vault.json` | ciphertext only |
| Places | `.playground/project.toml` `[[places]]` | no secrets |
| Shared capacity (opt in) | `.playground/capacity/<member>.json` | numbers and labels only |
| Service usage | `.playground/usage/services.jsonl` | numbers only |

`<data>` is `$PLAYGROUND_DATA_DIR`, else `$XDG_DATA_HOME/playground`, else `~/.local/share/playground`.

When `secret-tool` can store and read back a random passphrase, keys are encrypted with it.
OpenSSH unlocks them through a `SSH_ASKPASS` helper (`SSH_ASKPASS_REQUIRE=force`) that runs
`secret-tool lookup` and holds only the lookup attributes, never the passphrase. Passphrases travel
on stdin and stdout and never appear in argv. Without a keyring, keys fall back to file permissions
alone, which is the same protection as a stock `~/.ssh`, and `protection` reports which one is in
use. Private files are written with `mkstemp` (0600 from creation) and an atomic replace. Key files
that are links or belong to another user are refused.

## Server access: a short-lived SSH certificate per member

- Each project has its own CA, on the owner's device.
- `ProjectCA.issue` signs a member's registered device key with `-V -5m:+12h` (24 h at most), a
  serial, the key id `<member>@<device>`, and `-O clear` plus only the extensions needed.
  `permit-pty` is always on. `permit-port-forwarding` is on except in production-only
  certificates. Agent and X11 forwarding are never allowed.
- Principals are project scoped: `pg-<project id>-development|preview|production`. Owners get all
  three. Builders get development and preview. Reviewers and viewers get no shell. A certificate
  from one project is useless on another project's server, even when both trust their CAs on the
  same machine.
- Removing a member puts all of their device keys (`key:`) and certificate ids (`id:`) into the
  KRL. `ssh-keygen -Q` confirms it in tests. Adding them back lifts it.
- `server_setup_instructions(place)` returns:
  - the three sshd lines: `TrustedUserCAKeys`, `RevokedKeys` and `AuthorizedPrincipalsFile` under
    `/etc/ssh/playground/`;
  - the files and their contents;
  - an idempotent root script, and a one-line command the owner can paste:
    `ssh -t … 'printf %s <base64> | base64 -d | sudo sh'`.

  The script:
  - keeps a folder per project;
  - merges every project's CA into one trust file;
  - rebuilds one KRL from every project's spec;
  - writes a `sshd_config.d` drop-in;
  - refuses to change a server that already uses its own certificates;
  - runs `sshd -t` before reloading.

  Running it again after a removal pushes the revocation at once. Without a push, access still
  ends when the last certificate expires, within 12 hours.
- Tested end to end on both hosts with a real unprivileged `sshd`. A certificate login succeeds;
  after removal and re-running the script, the same certificate is refused.

## A shared project's home: every sign-in checked, removal at once

`core/sync.py` (design in plain words: `SHARING.md`). For the server a shared project lives on, the
static trust above (fixed principals per account, a KRL that needs the script re-run) isn't enough: a
removed builder could keep a shell for up to 12 hours, and from that shell edit anything the server
account owns. So the home works differently:

- **The access list is signed and expires.** Members, roles and each computer's public key, signed with
  SSHSIG by the project CA or by an owner computer the CA lists in a separate `issuers` file (signed by
  the CA only). It counts for 72 hours; any owner's Playground re-signs it with 48 hours left; the home
  refuses it after that (and refuses one claiming more than 96 hours). Lists only move forward: the
  pre-receive hook wants a newer version and never an older `issuers` file.
- **One root setup, root-owned checks** (`sync.live_setup_script`). sshd's `AuthorizedKeysCommand`
  (`/etc/ssh/playground/bin/keys`, run as root) reads the list at every sign-in. It reads the repository
  as the server account through `runuser` (so git never runs as root on files that account controls),
  then verifies every signature itself against the root-owned CA key. It emits `cert-authority` lines
  only: a sync pass is allowed only with `restrict` and exactly `command="<root gate> <member> <its own
  key's fingerprint>"` (a pass with any other forced command is refused by sshd, which demands they
  match); a shell only for a builder's or owner's computer on the current list. The gate and the hooks
  are root's too, and the gate makes git use root's hooks (`core.hooksPath`). Nothing the server account
  can write decides who gets in.
- **Removal is immediate** for syncing and shells, without running anything again. The KRL stays as a
  second lock.
- **Without root** (`trust = "account"`): the CA goes into the account's own `authorized_keys`, limited to
  the project's principals. Syncing is still checked at every connection by the gate. Shells end only when
  the 12-hour pass runs out, and anyone with a shell can change the account; the app says so in those
  words wherever it matters.
- **Signers.** Passes from an owner computer other than the CA count only with the root setup. A pass
  only ever works for a computer on the current list with that list's role, so a signer can't let anyone
  in on its own. A computer that stops being an owner's stays listed as `retired` for 7 days so the passes
  it signed keep working until fresh ones arrive; it can't sign a list.
- `ProjectCA.issue_sync` (sync passes), `ProjectCA.revoke_key` (one lost computer), `sign_certificate`,
  `shell_options`, `sync_options` and `Vault.remove_device` are new here. Sync passes' forced commands are
  written without quotes (every part matches a strict pattern), so the root check can repeat them exactly.
- Tested with a real unprivileged sshd on the Mac and in the VM, and in the VM with the setup really run
  as root (`tests/test_sync_root.py`): the checks are root's and not writable by the account, the key
  check run as root reads the project through `runuser`, a removed builder's shell and sync are refused at
  once, a list whose time is up is refused while a current one works, and a builder with a shell can't
  change the gate.

## The vault

- **Per secret version:** a fresh 256-bit content key and a ChaCha20-Poly1305 encryption of the
  value. The associated data binds the project, the name and the version, so ciphertexts can't be
  swapped between names.
- **Per recipient device:** an ephemeral X25519 key, then
  HKDF-SHA256(shared, salt = epk‖recipient, info = domain‖AAD), then ChaCha20-Poly1305 wraps the
  content key. The recipient id is bound in as associated data.
- **Authenticated membership.** The roster is an append-only log. Each entry is signed with
  Ed25519 by an admin of the *previous* entry; the first entry is self-signed by the creator.
  - Each device pins the first entry and the newest version and hash it has seen.
  - A recipient injected into the file, a roster entry signed by a non-admin, a rolled-back file
    or a rewritten history are all refused. This is what makes it end-to-end and not just
    encrypted: the relay (or anyone who can write the folder) can't add itself as a recipient.
  - Each secret is signed by its writer, who must be in the current roster.
- **Removal re-encrypts everything** under new content keys with bumped versions.
  `remove_member` returns the names the person could have read, so the UI can say "change these
  where they come from". Encryption can't take back what someone has already seen.
- **Plaintext guard:** before writing, `_save` refuses the write if any value it was handed (or
  its base64) would appear in the output.
- **Without python3-cryptography:** `status()` reports "The vault needs python3-cryptography…",
  every operation raises `VaultUnavailable`, and nothing is written. On the Mac host these tests
  skip; in the VM they run.
- **Not hidden:** secret *names* (so they can be listed while locked) and member names.

## Production protection

- `places.guard_write(place)` raises `ProductionProtected` for any production place unless it is
  given a `ReleaseTicket`.
- Only `authorize_release(project, review, place_id, approved_by)` makes a ticket, and only when:
  - the Review is `ready`;
  - its `destination` is that place;
  - the approver is an owner or a reviewer.
- Tickets are HMAC-SHA256'd with a key that exists only in the app process's memory, work once,
  expire after 10 minutes, and are logged to activity. An agent talks to Playground from its own
  process (MCP), so it can't mint one.
- Remote previews call `guard_write` too, so the Stage never runs on production.

## SSH hygiene (places, remote preview)

- Host, user and port are validated against strict patterns, and `--` separates options from the
  destination. A host like `-oProxyCommand=…` is refused.
- Host keys are trusted on first use, in Playground's own `known_hosts` plus `~/.ssh/known_hosts`.
  A changed key is reported as "This server's identity has changed … don't connect until you know
  why" and is never accepted.
- Connections never prompt: BatchMode, no passwords, no keyboard-interactive. With a
  keyring-encrypted device key, askpass is the only thing that can unlock it.

## Code we didn't write (preview, checks)

- The app being built and check commands run with a scrubbed environment: an allowlist of plain
  variables, minus anything matching API_KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL or AUTH. There
  is no `SSH_AUTH_SOCK`, so the app can't use the person's SSH agent.
- Each runs in its own process group, inside the project (a `run.toml` `cwd` outside it is
  refused), with timeouts and capped logs.
- Vault secrets reach the app only when the caller passes them (`extra_env`).
- Servers bind to 127.0.0.1 where the framework allows it.
- Sandboxing these processes (bubblewrap or Landlock) belongs to the agents sandbox module, not
  here.

## Service usage adapters (`core/services.py`)

- **Read only.** Postgres sessions are opened with `default_transaction_read_only=on` and a
  statement timeout.
- **Credentials:**
  - They come from the vault by the names listed on the service, and live only for one request.
  - They travel in a header, or in one child process's environment (`PGPASSWORD`, `AWS_*`), never
    in argv.
  - Child programs get a minimal environment and no inherited AWS config files.
- **Endpoints are constants in the code, never read from `stack.toml`.** Otherwise a teammate (or
  the relay) could point a vault token at their own server.
- Credentialed requests must be HTTPS, and redirects are refused. urllib would otherwise forward
  the `Authorization` header to the redirect's host, and a test proves the second host never sees
  it.
- Status text is scrubbed of every credential value (raw and URL-encoded) before it's stored or
  shown. Snapshots in the project are numbers only, and the store enforces that.

## Known limits and next steps

- **Revocation needs a push on servers that aren't a shared project's home** (Places' own servers,
  production). Until the setup script is re-run there, a removed member keeps access until their
  certificate expires (12 h at most). A shared project's home doesn't need it (see above).
- **A shell on the home can put an old access list back.** It works only until its time is up (72 h at
  most after it was signed), and an owner's open Playground replaces it within seconds. Whatever someone
  left running as the server account while they had a shell is theirs; check the account after removing
  someone you don't trust.
- **Trust on first use for the vault.** A new member pins whichever vault genesis they see first.
  Showing the owner's vault fingerprint during invite, and comparing it out of band or through
  Luma Connect, would close that gap.
- **Identity binding** to Luma Connect is a label today (`connect_id` in `device.json`). It should
  become a Connect-signed attestation of the device's public keys.
- **macOS and Windows keyrings** (Keychain, Credential Manager) aren't wired in. On those hosts keys
  fall back to file permissions.
- **Memory hygiene:** Python can't reliably wipe memory, so plaintext lives only as long as a call
  needs it.
- **Two admins changing the roster at once** on different devices produce a sync conflict that
  needs a manual merge. Each side stays valid on its own.
