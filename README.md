# vault-tui

A vi-modal terminal picker for a Bitwarden vault, built on [rbw](https://github.com/doy/rbw). Type to filter, see the entry before you commit to it, yank a field.

![python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## Why

`rbw get` wants the exact entry name. `rbw list | fzf` is searchable but shows nothing about an entry until you pick it, and then every field is another `rbw get` subprocess at roughly 306 ms each.

vault-tui decrypts rbw's local encrypted cache once, in-process, at startup. Measured on a 1218-entry vault:

| step | cost |
|---|---|
| PBKDF2 key derivation (once) | ~67-101 ms |
| decrypt all 1218 entries | ~8 ms |
| `rbw get` subprocess, per field | ~306 ms |

After unlock, everything is in memory. The detail pane fills as you move the cursor; there is no loading state anywhere in the UI.

## Layout sketch

Not a screenshot. Entry names are placeholders.

```
┌ / bank                                              NAVIGATION ┐
├──────────────────────────────┬─────────────────────────────────┤
│ > example-bank               │ name      example-bank          │
│   example-bank (joint)       │ user      alice@example.com     │
│   github.com                 │ password  ••••••••••••          │
│   ...                        │ totp      ••••••••••••          │
│                              │ url       https://example-bank  │
│                              │ folder    Finance               │
│                              │ notes     ...                   │
├──────────────────────────────┴─────────────────────────────────┤
│ 3/1218  j/k move  l open  y yank  r reveal  ? help  q quit     │
└────────────────────────────────────────────────────────────────┘
```

Top: the search field, a real prompt_toolkit `Buffer` running in vi mode. Left: the entry list, ranked by frecency and filtered live. Right: the fields of the selected entry, sensitive values masked. Bottom: match count, current mode, and the most common bindings.

## How it works

### Data flow

1. rbw syncs with the Bitwarden server. vault-tui never touches the network.
2. At startup vault-tui reads rbw's encrypted cache JSON (`~/Library/Caches/rbw/`) and config (`~/Library/Application Support/rbw/config.json`), read-only.
3. It prompts for the master password through pinentry, derives the keys, and decrypts every entry into memory.
4. Writes (edit, delete, sync) go through the rbw CLI. Afterwards the app re-unlocks and re-decrypts, so the in-memory view is always rbw's view.

### The design decision

The obvious implementation shells out to `rbw get` for each field on demand. That is simple and inherits rbw's crypto, but it puts a ~300 ms subprocess between every cursor movement and the detail pane.

Instead, vault-tui reads rbw's cache directly and re-implements Bitwarden's client-side decryption in `bin/vault_crypto.py` (365 lines, no UI imports). The trade-off is explicit: this is a second implementation of a security-sensitive path, and it is coupled to rbw's cache format. If rbw changes how it stores the cache, this tool has to follow. In exchange, the whole vault decrypts in single-digit milliseconds and the UI has no asynchronous state to manage.

### Crypto

All of this lives in `vault_crypto.py` and follows the Bitwarden client scheme.

- **Master key.** `PBKDF2-HMAC-SHA256(password, salt = lowercased account email, iterations)` produces a 32-byte master key. The iteration count comes from rbw's cache (600k on the author's account), not a hardcoded constant.
- **Key expansion.** `enc_key = HMAC(mk, "enc\x01")`, `mac_key = HMAC(mk, "mac\x01")`. This is Bitwarden's HKDF-expand step.
- **CipherString type 2** (AES-256-CBC + HMAC-SHA256). The MAC is verified with `hmac.compare_digest` before any decryption happens. PKCS7 padding is validated after.
- **CipherString type 4** (RSA-OAEP-SHA1). Used to unwrap organization keys with the account's RSA private key, which is itself stored as a type-2 string and loaded as DER/PKCS8.
- **Key precedence.** A per-cipher item key beats the organization key, which beats the account key. This matches Bitwarden.
- **Failure is a value.** Bad MAC, malformed base64, bad padding: every failure returns `None`, never raises. One corrupt entry cannot take down the UI.

Dependencies: the `cryptography` library for AES and RSA; stdlib `hashlib` and `hmac` for the rest.

### Unlock

The master password is collected via pinentry (`pinentry-mac` by default; rbw's configured pinentry is honored). The plaintext password is discarded immediately after key derivation. The one exception is the optional macOS Keychain step: on first run the tool asks a y/n question about storing the password in Keychain via the `security` CLI, and the password is held only while that prompt is open. A decline is remembered in a marker file so the question is asked once.

## Security model

What touches disk:

- rbw's encrypted cache and config, read-only.
- `~/.local/share/vault-frecency.json`: entry names and usage timestamps only. Mode 0600, written atomically.
- Optionally, the master password in macOS Keychain, only after explicit consent.

What does not:

- The decrypted vault. It lives in process memory and nowhere else. An earlier version wrote a plaintext field cache to `$TMPDIR`; the current version deletes that stale file at startup if it finds one.

Clipboard: yank copies via `pbcopy`. After 30 seconds the tool clears the clipboard, but only if it still holds the value that was copied. If you have copied something else since, it is left alone.

What is not claimed:

- **No memory wiping.** Keys and decrypted fields are ordinary Python `bytes`. Python offers no way to zero them, so they persist until garbage collection and may be visible to a process with memory access.
- **No auto-lock.** There is no session timeout. Once unlocked, the vault stays unlocked until you quit. This relies on the OS session lock and the Keychain ACL.

## Keys

vi modal: NAVIGATION and INSERT. Counts work where you would expect (`3j`). Press `?` in the app for the complete list.

| key | action |
|---|---|
| `j` / `k`, `ctrl-j` / `ctrl-k` | move down / up |
| `gg` / `G` | top / bottom |
| `ctrl-d` / `ctrl-u`, `ctrl-f` / `ctrl-b` | half page / full page |
| `l` or `Enter` | open the entry's fields |
| `h` or `Esc` | back to the list |
| `i` `a` `A` `I` | enter INSERT in the search field |
| `cc` or `S` | replace the query |
| `dd` | clear the query |
| `/` (inside an entry) | jump to a field by label or value, live |
| `y` | yank: the selected field in the fields pane, the password from the list |
| `r` | reveal / mask a sensitive field |
| `e` | edit via `rbw edit` in `$EDITOR` |
| `DD` then `Y` | arm delete, then confirm |
| `s` | force `rbw sync` |
| `?` | help overlay with every binding |
| `q` | quit |

Because the search field is a real vi buffer, `dw`, `cw`, undo, and paste all work in it and all re-filter the list as they change the text. Matching is a case-insensitive substring over name, user, and folder.

Ranking uses Mozilla-style frecency: each entry's score is the sum over its use timestamps of `0.5^(age / 30 days)`, capped at 40 timestamps per entry. An entry is bumped when opened (once per session) and on each yank.

## Install

Requirements: rbw configured and synced at least once; Python 3.9+; macOS.

```sh
git clone https://github.com/cybermelons/vault-tui
cd vault-tui
python3 -m venv .venv
.venv/bin/pip install prompt_toolkit cryptography
ln -s "$PWD/bin/vault-tui" ~/.local/bin/vault-tui
```

Symlink, do not copy: `bin/vault-tui` imports `vault_crypto.py` from its own directory. The shebang is `#!/usr/bin/env python3`, so either the `python3` on your PATH needs the two dependencies or you point the shebang at `.venv/bin/python3`.

### Self-test

```sh
vault-tui --check
```

Runs the built-in test suite against synthetic fixtures: freshly generated keys and ciphertexts, a fake pinentry script, an injected keychain function, and headless prompt_toolkit apps. It never reads the real vault, Keychain, or pinentry, so it is safe to run on any machine.

## Popup launcher (optional, macOS)

`bin/vault-popup` is a toggle script meant to be bound to a hotkey by skhd, Hammerspoon, or similar (the author uses alt-p). It needs yabai at `/opt/homebrew/bin/yabai`, Ghostty, and a Ghostty config you provide at `~/.config/ghostty/vault.conf`.

- Vault window on the current space: stash it on another space.
- Vault window elsewhere: pull it here.
- No vault window: spawn Ghostty running vault-tui, floated and centered via yabai.

The process stays alive between toggles, so re-opening is instant and does not re-prompt for the password.

## Limitations

- **macOS only as shipped.** Cache and config paths, `pbcopy`, the `security` CLI, and the `pinentry-mac` default are all macOS. Porting means changing two path constants in `vault_crypto.py` plus the clipboard command.
- **PBKDF2 accounts only.** Argon2id is not implemented. A vault using it will fail to decrypt.
- **CipherString types 2 and 4 only.** Other types are not handled.
- **No TOTP generation.** The `totp` field shows and copies the stored seed, not a current code.
- **No auto-lock.** See the security model above.
- **Single rbw account.**
- **Edit is bounded by `rbw edit`**, which covers password and notes only.
- **One data point for performance.** All numbers above come from one ~1.2k-entry vault on the author's machine.

## Files

```
bin/vault-tui         1737  main app: layout, bindings, state, unlock flow,
                            clipboard, edit/delete/sync via rbw CLI, --check suite
bin/vault_crypto.py    365  pure crypto and IO, no UI imports
bin/vault-frecency      76  frecency ranking CLI: bump, rank
bin/vault-popup         70  macOS toggle launcher (yabai + Ghostty)
bin/vault-detail        64  standalone helper: flattens `rbw get --raw --full`
                            JSON into label/value rows; not used by the TUI path
```

About 2.4k lines total.
