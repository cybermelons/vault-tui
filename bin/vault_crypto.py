#!/usr/bin/env python3
"""In-process decryption of rbw's own cache db. Pure functions, no UI imports.

Bitwarden crypto, minimum needed to read (never write) a vault:
  - kdf=0 PBKDF2-SHA256(password, salt=email.lower(), iterations) -> master key
  - HKDF-expand(master key) -> (enc key, mac key) via HMAC(mk, b"enc\\x01" / b"mac\\x01")
  - protected_key unwraps with those to 64 bytes -> account (sym_enc, sym_mac)
  - CipherString "2.<iv>|<ct>|<mac>" b64 parts: AES-256-CBC + HMAC-SHA256, PKCS7.
    MAC is verified (constant-time) before decrypting; mismatch -> None, never raise.
  - Per-cipher key (entry.key, type "2."): unwrap with account keys -> 64 bytes,
    use THOSE for that entry's fields instead of the account keys.
  - Org entries (entry.org_id set): org sym key comes from
    protected_org_keys[org_id], type "4." (RSA-OAEP-SHA1, no IV/HMAC — pure
    asymmetric blob), unwrapped with the account RSA private key, which is
    itself protected_private_key (type "2.") unwrapped with account keys.

Measured on the real vault: PBKDF2(600k) 67-101ms once; then ~8ms to decrypt
all 1218 personal entries (vs. 306ms/field via `rbw get` subprocess).
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_private_key

CONFIG_PATH = os.path.expanduser("~/Library/Application Support/rbw/config.json")
CACHE_DIR = os.path.expanduser("~/Library/Caches/rbw")

LOGIN = ["username", "password", "totp"]
CARD = ["cardholder_name", "brand", "number", "code", "exp_month", "exp_year"]
IDENT = ["title", "first_name", "middle_name", "last_name", "username", "email",
         "phone", "address1", "address2", "address3", "city", "state",
         "postal_code", "country", "ssn", "license_number", "passport_number"]


def load_config():
    with open(CONFIG_PATH) as fh:
        cfg = json.load(fh)
    return cfg["email"], cfg.get("pinentry") or "pinentry-mac"


class PinentryCancelled(Exception):
    """User dismissed the pinentry prompt."""


def _assuan_escape(s):
    return s.replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")


def prompt_password(pinentry_path="pinentry-mac", desc="Unlock vault-tui",
                     prompt="Master password:"):
    """Drive pinentry's Assuan stdin/stdout protocol for one password.

    Never touches our own terminal (the TUI owns it). Raises
    PinentryCancelled if the user cancels; any protocol error surfaces as
    that too, so the caller has one exit path.
    """
    proc = subprocess.Popen([pinentry_path], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    try:
        def send(cmd):
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            lines = []
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise PinentryCancelled("pinentry closed unexpectedly")
                line = line.rstrip("\n")
                if line.startswith("OK"):
                    return lines
                if line.startswith("ERR"):
                    raise PinentryCancelled(line)
                if line.startswith("D "):
                    lines.append(line[2:])
                # ignore S (status) / # (comment) lines

        proc.stdout.readline()  # initial greeting ("OK Pleased to meet you...")
        send(f"SETDESC {_assuan_escape(desc)}")
        send(f"SETPROMPT {_assuan_escape(prompt)}")
        data = send("GETPIN")
        pin = data[0] if data else ""
        try:
            send("BYE")
        except PinentryCancelled:
            pass
        return pin
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


NO_KEYCHAIN_MARKER = os.path.expanduser("~/.local/share/vault-tui/no-keychain")

# SECURITY NOTE: storing the master password in the Keychain means any
# process running as this user can read it straight back with the same
# `security find-generic-password -w` command — no prompt required, once
# the item's ACL allows this tool to read it. That is comparable exposure
# to rbw's own already-unlocked agent socket (also readable by anything
# running as the user). It's a deliberate trade — no prompt on a normal
# launch — made explicitly by answering "y" to the one-time consent
# prompt, not a hidden default.


def keychain_get(email, service="vault-tui"):
    """Read-only Keychain lookup: `security find-generic-password -w`.

    Never writes, adds, or deletes a Keychain item — read-only. ANY
    failure (item not found, user denies the access-control dialog,
    security(1) missing, non-zero exit) is a plain miss: return None,
    never fatal. The caller falls through to pinentry.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", email, "-w"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    pw = r.stdout.rstrip("\n")
    return pw or None


def keychain_set(email, password, service="vault-tui"):
    """Store the master password in the Keychain: `security add-generic-password`.

    Only called after the user explicitly consents (the TUI's one-time
    status-line prompt). `-U` updates in place if somehow already present.
    Never call security delete-generic-password from here or anywhere in
    this codebase — removal is the user's business, done by hand.
    Raises on failure; the caller must catch it and surface the error
    without treating it as fatal (the vault is already unlocked by then).
    """
    r = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", email,
         "-w", password, "-D", "vault-tui master password", "-U"],
        capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "security add-generic-password failed").strip())


def has_declined_keychain():
    return os.path.exists(NO_KEYCHAIN_MARKER)


def record_keychain_decline():
    """Write the marker that stops the save-to-Keychain prompt forever."""
    try:
        os.makedirs(os.path.dirname(NO_KEYCHAIN_MARKER), exist_ok=True)
        with open(NO_KEYCHAIN_MARKER, "w") as fh:
            fh.write("user declined Keychain storage for vault-tui\n")
    except OSError:
        pass  # best-effort; worst case it asks again next launch


def get_master_password(email, pinentry_path="pinentry-mac",
                         keychain_get_fn=keychain_get):
    """Keychain first, pinentry fallback. -> (password, from_keychain: bool).

    from_keychain tells the caller whether to offer the save-to-Keychain
    consent step at all (only relevant right after a pinentry unlock).
    Raises PinentryCancelled if the Keychain has nothing AND the pinentry
    prompt is cancelled/fails — that's the single exit path callers need
    to handle. keychain_get_fn is injectable so --check never touches the
    real Keychain.
    """
    pw = keychain_get_fn(email)
    if pw:
        return pw, True
    pw = prompt_password(pinentry_path, desc=f"Unlock vault for {email}",
                         prompt="Master password")
    return pw, False


def load_db(email):
    path = os.path.join(CACHE_DIR, f"default:{email}.json")
    with open(path) as fh:
        return json.load(fh)


def derive_keys(password, email, iterations):
    """-> (enc, mac) account symmetric keys, from the master password."""
    mk = hashlib.pbkdf2_hmac("sha256", password.encode(), email.lower().encode(),
                              iterations)
    enc = hmac.new(mk, b"enc\x01", hashlib.sha256).digest()
    mac = hmac.new(mk, b"mac\x01", hashlib.sha256).digest()
    return enc, mac


def unwrap(cipherstring, enc, mac):
    """CipherString "2.<iv>|<ct>|<mac>" -> plaintext bytes, or None (bad MAC /
    malformed / wrong key). Never raises."""
    if not cipherstring or not isinstance(cipherstring, str):
        return None
    if not cipherstring.startswith("2."):
        return None
    try:
        iv_b64, ct_b64, mac_b64 = cipherstring[2:].split("|")
        iv, ct, want_mac = (base64.b64decode(x) for x in (iv_b64, ct_b64, mac_b64))
        got_mac = hmac.new(mac, iv + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(got_mac, want_mac):
            return None
        c = Cipher(algorithms.AES(enc), modes.CBC(iv)).decryptor()
        pt = c.update(ct) + c.finalize()
        if not pt:
            return None
        pad = pt[-1]
        if pad < 1 or pad > 16 or pad > len(pt):
            return None
        return pt[:-pad]
    except Exception:
        return None


def unwrap_text(cipherstring, enc, mac):
    raw = unwrap(cipherstring, enc, mac)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


def _unwrap_rsa_oaep(blob, private_key):
    """Type "4." CipherString: RSA-OAEP-SHA1, no IV, no HMAC — pure asymmetric."""
    if not blob or not blob.startswith("4."):
        return None
    try:
        ct = base64.b64decode(blob[2:])
        return private_key.decrypt(ct, padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(), label=None))
    except Exception:
        return None


def account_private_key(protected_private_key, enc, mac):
    """protected_private_key (type "2.") -> loaded RSA private key, or None."""
    der = unwrap(protected_private_key, enc, mac)
    if der is None:
        return None
    try:
        return load_der_private_key(der, password=None)
    except Exception:
        return None


def org_sym_keys(protected_org_keys, private_key):
    """{org_id: (enc, mac)} for every org key that unwraps cleanly."""
    out = {}
    if private_key is None:
        return out
    for org_id, blob in (protected_org_keys or {}).items():
        raw = _unwrap_rsa_oaep(blob, private_key)
        if raw is not None and len(raw) >= 64:
            out[org_id] = (raw[:32], raw[32:64])
    return out


def entry_keys(entry, sym, org_keys):
    """-> (enc, mac) to use for this entry's own fields.

    Precedence: per-cipher entry.key (unwrapped with the account keys) wins
    over org, matching rbw/Bitwarden's own resolution — an entry can belong
    to an org and still carry its own key.
    """
    if entry.get("key"):
        raw = unwrap(entry["key"], *sym)
        if raw is not None and len(raw) >= 64:
            return raw[:32], raw[32:64]
    org_id = entry.get("org_id")
    if org_id and org_id in org_keys:
        return org_keys[org_id]
    return sym


def _rows_from_data(data, enc, mac):
    """dict like {"Login": {...}} / {"Card": {...}} -> [(label, value), ...]."""
    rows = []
    if not isinstance(data, dict) or not data:
        return rows
    inner = next(iter(data.values()))
    if not isinstance(inner, dict):
        return rows

    def add(label, cs):
        v = unwrap_text(cs, enc, mac)
        if v is not None and v.strip():
            rows.append((label, v))

    seen = set()
    for k in LOGIN + CARD + IDENT:
        if k in inner and k not in seen:
            seen.add(k)
            add(k.replace("_", " "), inner[k])

    for u in inner.get("uris") or []:
        add("uri", (u or {}).get("uri"))

    return rows


def decrypt_entry(entry, sym, org_keys):
    """One entry -> (name, user, folder, [(label, value), ...]) | None on failure."""
    enc, mac = entry_keys(entry, sym, org_keys)
    name = unwrap_text(entry.get("name"), enc, mac)
    if name is None:
        return None

    data = entry.get("data")
    rows = _rows_from_data(data, enc, mac) if isinstance(data, dict) else []

    user = ""
    for label, val in rows:
        if label == "username":
            user = val
            break

    for f in entry.get("fields") or []:
        label = unwrap_text(f.get("name"), enc, mac) or "field"
        val = unwrap_text(f.get("value"), enc, mac)
        if val is not None and val.strip():
            rows.append((label, val))

    folder = unwrap_text(entry.get("folder"), enc, mac) if entry.get("folder") else None
    if folder:
        rows.append(("folder", folder))

    notes = unwrap_text(entry.get("notes"), enc, mac) if entry.get("notes") else None
    if notes and notes.strip():
        rows.append(("notes", notes.replace("\n", "  ⏎  ")))

    return name, user, folder or "", rows


def decrypt_all(db, sym, org_keys):
    """-> ({(name, user): [(label, value), ...]}, skipped_count).

    Keyed by (name, user) to match the existing cache contract; name keeps
    its original leading whitespace/tabs (never stripped — it's a cache key).
    On a (name, user) collision (rare: two entries can share both), the
    later entry wins, same as the old rbw-list-driven cache did.
    """
    out = {}
    skipped = 0
    for entry in db.get("entries") or []:
        result = decrypt_entry(entry, sym, org_keys)
        if result is None:
            skipped += 1
            continue
        name, user, _folder, rows = result
        out[(name, user)] = rows
    return out, skipped
