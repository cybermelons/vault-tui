# vault-tui

A modal terminal browser for [Bitwarden](https://bitwarden.com), built on
[`rbw`](https://github.com/doy/rbw) and `prompt_toolkit`. Vi keybindings,
lazy decryption, and a frecency-ranked entry list.

```
┌─ search ─────────────────────────────────────────┐
│ /git                                             │
├──────────────────────┬───────────────────────────┤
│ ● github.com         │ username  you@example.com │
│   gitlab.com         │ password  ••••••••  (y)   │
│   git.sr.ht          │ totp      123456          │
└──────────────────────┴───────────────────────────┘
```

## Why it exists

`rbw get` is fast but you have to know the entry name. `rbw list | fzf` is
searchable but re-shells per lookup and shows nothing about the entry until
you pick one. This keeps a persistent TUI with a detail pane that fills in as
you move, without decrypting the whole vault up front.

## Design notes

**Lazy hydration around the cursor.** Decrypting every entry at startup means
a multi-second wait on a large vault. Instead a single background worker
decrypts the rows nearest the cursor and backfills outward, so the pane you
are looking at is usually already warm.

**One worker, deliberately.** An early version used a thread pool, on the
assumption that parallel decrypts would be faster. They are not — `rbw`
serialises decryption server-side, so additional workers only add contention.
The slot-and-worker design replaced it.

**Errors are never cached.** A failed decrypt used to be written into the
cache like any other result, so one transient failure would show
"(no fields)" for that entry until restart. Failures now surface and are
retried.

**Frecency ranking.** `vault-frecency` scores entries by recency and
frequency of use so the list orders itself around how you actually work.

## Install

Requires `rbw` (configured and unlocked at least once) and Python 3.9+.

```sh
pip install prompt_toolkit
git clone https://github.com/cybermelons/vault-tui
ln -s "$PWD/vault-tui/bin/vault-tui" ~/.local/bin/vault-tui
```

## Keys

| key | action |
|---|---|
| `j` / `k`, `ctrl-j` / `ctrl-k` | move |
| `/` | search; `dd` clears it |
| `y` | yank password, or the selected field |
| `a` / `A` | append after cursor / at end of line |
| `q` | quit |

## Layout

| file | role |
|---|---|
| `bin/vault-tui` | the TUI: layout, keymap, slot+worker cache |
| `bin/vault-detail` | decrypt one entry's fields |
| `bin/vault-frecency` | usage-ranked ordering |
| `bin/vault-popup` | float it in a terminal window (macOS/yabai) |
| `bin/vault-warm` | pre-warm the rbw agent |

## Status

Personal tool, used daily. `vault-popup` assumes macOS and
[yabai](https://github.com/koekeishiya/yabai); everything else is portable.
