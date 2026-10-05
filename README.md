# vault-tui

A vi-modal terminal picker for a Bitwarden vault, built on the official [Bitwarden CLI](https://bitwarden.com/help/cli/)'s `bw serve` local API. Type to filter, see the entry before you commit to it, yank a field.

![python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## Why

`bw get` wants the exact entry name. `bw list | fzf` is searchable but shows nothing about an entry until you pick it, and then every field is another `bw` subprocess.

vault-tui instead starts (or reuses) a local `bw serve` instance and pulls the whole vault once, at startup, with a single `GET /list/object/items`. Every item comes back already plaintext — `bw serve` owns the vault's crypto, not vault-tui — so there is no local decryption step and no cache format to track.

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

1. At startup vault-tui checks `http://localhost:8087/status`. If nothing answers, it spawns `bw serve --hostname localhost --port 8087` itself (detached, logged to `~/Library/Logs/vault-tui-bw-serve.log`) and waits for it to come up.
2. If the vault is locked, it collects the master password (Keychain first, pinentry fallback — see Unlock below) and `POST`s it to `/unlock`.
3. It pulls `GET /list/object/items` and `GET /list/object/folders` once and builds the in-memory row cache from the plaintext JSON `bw serve` returns.
4. Writes (edit, delete, sync) go through the same API. Any call that comes back `"Vault is locked."` (e.g. the vault was locked externally, such as by `vault-popup`'s lock-on-hide) triggers one unlock-and-retry, automatically.

### The design decision

`bw serve`'s GET → modify → PUT round trip was verified (live, against a real vault) to preserve every field on a write, including passkeys and the item's own per-item encryption key — see History below for why that matters. Since `bw serve` also returns already-plaintext JSON, there is no decryption code to maintain at all: it is the one process that owns the vault's crypto, and vault-tui is a thin stdlib HTTP client in front of it (`urllib.request`, no third-party dependencies).

### Unlock

The master password is collected via pinentry (`pinentry-mac` by default, overridable with `VAULT_TUI_PINENTRY`), or read from the macOS Keychain if a password was saved there previously.

A wrong password is simply rejected by `POST /unlock`; the tool retries, up to 3 attempts total. A password that came from a stale Keychain entry is treated specially — after one failure it stops trying the Keychain and falls back to a real pinentry prompt, so a bad saved password can't lock the user out. Only after 3 failed attempts does it give up and report the vault as still locked, rather than silently rendering an empty vault.

The plaintext password is discarded immediately after a successful unlock. The one exception is the optional macOS Keychain step: on first run (after a password collected via pinentry) the tool asks a y/n question about storing the password in Keychain via the `security` CLI, and the password is held only while that prompt is open. A decline is remembered in a marker file so the question is asked once.

## Security model

What touches disk:

- `~/.local/share/vault-frecency.json`: entry names and usage timestamps only. Mode 0600, written atomically.
- Optionally, the master password in macOS Keychain, only after explicit consent.
- `~/Library/Logs/vault-tui-bw-serve.log`, only if vault-tui spawned `bw serve` itself.

What does not:

- The decrypted vault. It lives in process memory (the in-memory row cache built from `bw serve`'s responses) and nowhere else.

Clipboard: yank copies via `pbcopy`. After 30 seconds the tool clears the clipboard, but only if it still holds the value that was copied. If you have copied something else since, it is left alone.

Masking: a Bitwarden **hidden** custom field (field type 1) is masked because the vault says so, tagged when the row cache is built rather than guessed at display time. Everything else — password, totp, and other built-ins that carry no field type — falls back to a label-text heuristic (`password`, `totp`, etc.). `r` reveals the selected field for the rest of the session.

What is not claimed:

- **No memory wiping.** Decrypted fields arrive from `bw serve` as ordinary Python `str`/`dict` values. Python offers no way to zero them, so they persist until garbage collection and may be visible to a process with memory access.
- **No auto-lock from vault-tui itself.** There is no session timeout inside the app. `bin/vault-popup` locks the vault (`POST /lock`) whenever it hides the window, so the common hotkey-toggle workflow does re-lock on dismiss — but running `vault-tui` directly (outside the popup) leaves the vault unlocked until you quit or lock it another way.

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
| `e` | edit — all fields, as JSON, in `$EDITOR` |
| `DD` then `Y` | arm delete, then confirm |
| `s` | force sync (`POST /sync`) |
| `?` | help overlay with every binding |
| `q` | hide the popup window (alt-p to reopen instantly); quits outside the popup |
| `Q` | quit |

Because the search field is a real vi buffer, `dw`, `cw`, undo, and paste all work in it and all re-filter the list as they change the text. Matching is a case-insensitive substring over name, user, and folder.

Ranking uses Mozilla-style frecency: each entry's score is the sum over its use timestamps of `0.5^(age / 30 days)`, capped at 40 timestamps per entry. An entry is bumped when opened (once per session) and on each yank.

## Install

Requirements: the [Bitwarden CLI](https://bitwarden.com/help/cli/) (`bw`) installed and logged in (`bw login`) at least once; Python 3.9+; macOS.

```sh
git clone https://github.com/cybermelons/vault-tui
cd vault-tui
python3 -m venv .venv
.venv/bin/pip install prompt_toolkit
ln -s "$PWD/bin/vault-tui" ~/.local/bin/vault-tui
```

No third-party dependency besides `prompt_toolkit` — the `bw serve` client is stdlib `urllib.request`/`json` only. The shebang is `#!/usr/bin/env python3`, so either the `python3` on your PATH needs `prompt_toolkit` or you point the shebang at `.venv/bin/python3`.

vault-tui manages `bw serve` itself: if nothing answers on `localhost:8087` at startup, it spawns `bw serve --hostname localhost --port 8087` detached and waits for it to come up. You still need to have run `bw login` yourself at least once so the CLI has an account to unlock.

### Self-test

```sh
vault-tui --check
```

Runs the built-in test suite: row-building over sample Bitwarden item JSON, a locked→unlock→retry check against an in-process `http.server` stub, a fake pinentry script, an injected keychain function, and headless prompt_toolkit apps. It never touches the real vault, `bw serve`, Keychain, or pinentry, so it is safe to run on any machine.

## Popup launcher (optional, macOS)

`bin/vault-popup` is a toggle script meant to be bound to a hotkey by skhd, Hammerspoon, or similar (the author uses alt-p). It needs yabai at `/opt/homebrew/bin/yabai`, Ghostty, and a Ghostty config you provide at `~/.config/ghostty/vault.conf`.

- Vault window visible on the current space: hide it (instant, like cmd-H, via `NSRunningApplication`) and lock the vault (`POST localhost:8087/lock`, fire-and-forget) — only that Ghostty process, not other Ghostty windows.
- Vault window hidden or on another space: unhide it, pull it to the current space, and focus it.
- No vault window: spawn Ghostty running vault-tui, floated and centered via yabai.

The process stays alive between toggles, so re-opening is instant — except for re-authenticating, since hiding the window locks the vault. Re-opening re-prompts for the master password (Keychain first, so usually no pinentry dialog) and re-unlocks before showing the list.

## Limitations

- **macOS only as shipped.** `pbcopy`, the `security` CLI, and the `pinentry-mac` default are all macOS. Porting means changing the clipboard command and the pinentry default.
- **Single `bw` account**, whatever `bw` is currently logged into.
- **One data point for performance.** The 1218-entry vault figures in earlier versions of this doc came from one vault on the author's machine; this version's bottleneck is one HTTP round trip per list/get/put, not in-process crypto, so the numbers no longer apply as stated.

## Files

```
bin/vault-tui         2292  main app: layout, bindings, state, bw serve client,
                            unlock flow, clipboard, edit/delete/sync, --check suite
bin/vault-frecency      76  frecency ranking CLI: bump, rank
bin/vault-popup         91  macOS toggle launcher (yabai + Ghostty), locks on hide
```

### History

Earlier versions used the `rbw` CLI plus an in-process re-implementation of Bitwarden's client-side crypto (`bin/vault_crypto.py`) to read `rbw`'s local cache directly, avoiding a subprocess per field. That path was replaced by the `bw serve` HTTP client above after `rbw edit` was found to drop passkeys and per-item keys on save.
