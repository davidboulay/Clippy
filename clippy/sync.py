"""Encrypted LAN clipboard sync — the portable core.

Lives in the long-running daemon (it owns keys, peers, the listening socket and
mDNS). New local copies are broadcast to paired peers; received items are stored
and injected into the local clipboard. All payloads are encrypted+authenticated
with NaCl Box between paired X25519 identities. Discovery is mDNS (zeroconf).
Pairing is a short code-authenticated public-key exchange (SAS-style), so a
man-in-the-middle can't substitute a key.

GTK-free: runs on the headless macOS daemon too.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable, Dict, Optional

from . import config, settings, storage

_IMPORT_ERROR = ""
try:
    from nacl.public import Box, PrivateKey, PublicKey
    _HAVE_NACL = True
except Exception as _e:  # pragma: no cover - dependency missing
    _HAVE_NACL = False
    _IMPORT_ERROR += f"nacl: {_e!r}  "

try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
    _HAVE_ZC = True
except Exception as _e:  # pragma: no cover
    _HAVE_ZC = False
    _IMPORT_ERROR += f"zeroconf: {_e!r}"

try:
    from spake2 import SPAKE2_A, SPAKE2_B
    _HAVE_SPAKE2 = True
except Exception as _e:  # pragma: no cover - dependency missing
    _HAVE_SPAKE2 = False
    _IMPORT_ERROR += f"spake2: {_e!r}  "


def import_error() -> str:
    """Why sync is unavailable (the real ImportError), for diagnostics."""
    return _IMPORT_ERROR.strip()

PROTO = 1
# Pairing protocol version. v1 was a symmetric HMAC the code-holder disclosed in
# the clear and then accepted echoed back — knowledge of the code was never
# actually proven to the code-showing side, so any LAN host could pair without
# it. v2 runs SPAKE2 (a PAKE) keyed by the code: neither side transmits anything
# an attacker can replay, and a wrong code yields a different session key, so the
# key-confirmation below cannot pass without the code. The version is exchanged
# so a v2 device pairing with a v1 one fails with a clear "update" message
# instead of a confusing error.
_PAIR_PROTO = 2
_PAIR_TRANSCRIPT = b"clippy-pair-v2"
_PAIR_TIMEOUT = 120          # seconds a shown code stays valid
_PAIR_MAX_ATTEMPTS = 5       # failed guesses before the code is burned
_CONN_TIMEOUT = 5
_MAX_PAIR_MSG = 4096         # pre-auth frames (pair_*) are tiny; cap hard
_MAX_CONN = 16              # concurrent inbound connections; excess is dropped
# Liveness. "Online" used to mean only "mDNS currently advertises this peer",
# which is push-based and can lag reality (a crashed peer stays advertised until
# its record ages out). Instead we actively reach out on a timer and remember
# when each peer last answered, so the status bulb reflects real reachability and
# the UI can say when it was last confirmed.
_LIVENESS_INTERVAL = 15     # seconds between reachability sweeps
_LIVENESS_WINDOW = 40       # a peer is "online" if it answered within this many s
_SEEN_MAX = 256
_SEEN_TTL = 30               # seconds a hash stays "seen". Long enough to absorb
                             # the sync echo (a peer injects a received clip into
                             # its own clipboard, which would otherwise bounce
                             # straight back), short enough that deliberately
                             # re-copying an item later re-syncs instead of being
                             # silently suppressed forever.
_SEND_ATTEMPTS = 3           # text delivery retries before giving up
_SEND_BACKOFF = 0.4          # seconds between attempts (grows per round)


def sync_available() -> bool:
    return _HAVE_NACL and _HAVE_ZC and _HAVE_SPAKE2


_SYNC_LOG = config.DATA_DIR / "sync.log"


def _log(msg: str) -> None:
    """Append a diagnostic line to <data>/sync.log (GUI apps swallow stdout).

    Every delivery is recorded, successes included — a log that only spoke up on
    retries and failures made a healthy idle sync and a dead one look identical,
    and reading that silence as "nothing was even attempted" cost two wrong
    diagnoses. Self-bounds the file so it can't grow without limit.

    Owner-only (0600), like the identity key and peer list beside it. No clip
    contents are written, but the lines do name paired devices, their addresses
    and copied filenames — session metadata that other local accounts have no
    business reading.
    """
    try:
        _SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _SYNC_LOG.stat().st_size > 512 * 1024:
                _SYNC_LOG.write_bytes(_SYNC_LOG.read_bytes()[-256 * 1024:])
        except OSError:
            pass
        # Create it 0600 rather than at the process umask, and repair an existing
        # file left world-readable by an earlier version.
        fd = os.open(str(_SYNC_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
                     .encode("utf-8", "replace"))
        finally:
            os.close(fd)
        try:
            if (_SYNC_LOG.stat().st_mode & 0o077) != 0:
                os.chmod(_SYNC_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass


# --- framing ---------------------------------------------------------------
def _send_frame(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _unhex(s) -> Optional[bytes]:
    """Decode a hex string from an untrusted frame, or None if it isn't valid
    hex. Keeps peer-supplied fields from raising out of a handler."""
    if not isinstance(s, str):
        return None
    try:
        return bytes.fromhex(s)
    except ValueError:
        return None


def _recv_frame(sock: socket.socket, limit: int = 64 * 1024 * 1024) -> Optional[dict]:
    hdr = _recv_exact(sock, 4)
    if not hdr:
        return None
    (length,) = struct.unpack(">I", hdr)
    if length <= 0 or length > limit:
        return None
    body = _recv_exact(sock, length)
    if body is None:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    # A JSON body of `5` or `[1]` parses fine but isn't a frame; every caller
    # does obj.get(...), which would raise on a non-dict. Reject here so one
    # malformed frame can't crash a handler.
    return obj if isinstance(obj, dict) else None


def _send_raw(sock: socket.socket, data: bytes) -> None:
    """Length-prefixed raw bytes frame (used for streamed media chunks)."""
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_raw(sock: socket.socket) -> Optional[bytes]:
    hdr = _recv_exact(sock, 4)
    if not hdr:
        return None
    (length,) = struct.unpack(">I", hdr)
    if length == 0:
        return b""                       # end-of-stream marker
    if length > 64 * 1024 * 1024:
        return None
    return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _name_with_ext(name: str, mime: str) -> str:
    """Ensure a filename carries an extension matching its MIME type. Apps and
    file managers rely on the extension to recognize the type, and content
    copied as data (e.g. a screenshot) often arrives with a name that has none."""
    import mimetypes
    import os
    name = name or "file"
    if os.path.splitext(name)[1]:
        return name
    ext = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ""
    return name + ext


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _fp_of(pubkey_hex: str) -> str:
    """Short fingerprint of an X25519 public key (same form as the TXT ``fp``
    advertised over mDNS and ``SyncEngine.fingerprint``). Empty on bad input."""
    try:
        return hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()[:16] if pubkey_hex else ""
    except ValueError:
        return ""


class SyncEngine:
    def __init__(self, on_status: Optional[Callable[[], None]] = None,
                 port: Optional[int] = None, state_dir=None,
                 on_progress: Optional[Callable] = None):
        self._on_status = on_status
        self._on_progress = on_progress   # (name, sent, total, done) for big sends
        # Called (no args) after a *received* clip is stored + put on the local
        # clipboard. macOS uses it for the copy sound, because its changeCount
        # watcher is (correctly) suppressed for our own writes so capture_current
        # — which normally plays the sound — never runs for received clips. On
        # Linux the wl-paste watch re-fires capture, so it leaves this unset.
        self._on_received: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._server: Optional[socket.socket] = None
        self._running = False
        self._zc = None
        self._info = None
        self._browser = None
        self._peers_online: Dict[str, tuple] = {}   # id -> (ip, port, name)
        self._pairing = None                          # dict while in pairing mode
        self._pair_lock = threading.Lock()            # guards _pairing mutations
        self._conn_slots = threading.Semaphore(_MAX_CONN)

        self.port = port or config.SYNC_PORT
        # Paths are overridable so tests can run two engines in one process.
        from pathlib import Path
        base = Path(state_dir) if state_dir else None
        self._key_path = (base / "identity.key") if base else config.KEY_PATH
        self._peers_path = (base / "peers.json") if base else config.PEERS_PATH
        self._device_id_path = (base / "device-id") if base else config.DEVICE_ID_PATH
        if base:
            base.mkdir(parents=True, exist_ok=True)

        self.device_id = self._load_device_id()
        self._priv = self._load_identity()
        self.pubkey_hex = bytes(self._priv.public_key).hex() if self._priv else ""
        # trusted: peers paired under the current (SPAKE2) protocol, usable for
        # sync. stale: peers paired under the old handshake that had the auth
        # bypass — their trust was established insecurely, so it is disabled and
        # the user is asked to re-pair. See _load_peers.
        self.trusted, self.stale_peers = self._load_peers()
        self._last_seen: Dict[str, float] = {}        # id -> monotonic-ish last reply
        self._last_check = 0.0                         # wall time of the last sweep
        self._live_thread = None

    # -- identity / peers ------------------------------------------------
    def device_name(self) -> str:
        return settings.get("device_name") or socket.gethostname()

    def _load_device_id(self) -> str:
        config.ensure_dirs()
        p = self._device_id_path
        if p.exists():
            return p.read_text().strip()
        did = uuid.uuid4().hex
        p.write_text(did)
        return did

    def _load_identity(self):
        if not _HAVE_NACL:
            return None
        config.ensure_dirs()
        p = self._key_path
        if p.exists():
            return PrivateKey(p.read_bytes())
        priv = PrivateKey.generate()
        p.write_bytes(bytes(priv))
        os.chmod(p, 0o600)
        return priv

    def _load_peers(self):
        """Return (trusted, stale). A stored peer counts as trusted only if it
        was paired under the current pairing protocol (``proto >= _PAIR_PROTO``).

        Anything older was paired by the pre-SPAKE2 handshake, whose code check
        could be bypassed — so that trust was never securely established. Rather
        than silently keep using it (which would carry the vulnerability's
        consequences past the fix) or silently delete it (sync just stops, with
        no explanation), it is set aside as ``stale``: disabled for sync, but
        remembered so the UI can ask the user to re-pair that device once."""
        raw = self._load_peers_raw()
        trusted, stale = {}, {}
        for pid, entry in raw.items():
            if isinstance(entry, dict) and int(entry.get("proto", 1)) >= _PAIR_PROTO:
                trusted[pid] = entry
            else:
                stale[pid] = entry
        if stale:
            _log(f"pairing: {len(stale)} device(s) paired before the security "
                 f"update are disabled pending re-pair: "
                 f"{', '.join(e.get('name', i) for i, e in stale.items())}")
        return trusted, stale

    def _load_peers_raw(self) -> Dict[str, dict]:
        try:
            return json.loads(self._peers_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_peers(self) -> None:
        # Persist stale (not-yet-re-paired) entries alongside trusted ones, so
        # the "re-pair required" prompt survives restarts until the user acts.
        # trusted wins on any id collision (a completed re-pair supersedes the
        # stale record). Written 0600 like the identity key beside it.
        combined = dict(self.stale_peers)
        combined.update(self.trusted)
        fd = os.open(str(self._peers_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(combined, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(self._peers_path, 0o600)
        except OSError:
            pass

    def fingerprint(self) -> str:
        return _fp_of(self.pubkey_hex)

    def unpair(self, peer_id: str) -> bool:
        """Forget a paired device (drops trust + any live discovery entry)."""
        removed = self.trusted.pop(peer_id, None) is not None
        self._peers_online.pop(peer_id, None)
        if removed:
            self._save_peers()
        return removed

    def _adopt_peer_id(self, fp: str, new_id: str) -> Optional[dict]:
        """Reconcile a trusted peer onto its current ``device_id``.

        A peer's ``device-id`` file can regenerate while its keypair (hence
        fingerprint) stays the same, so trust must follow the stable key, not
        the random id. Given an advertised/observed fingerprint and the peer's
        current device_id, collapse every trusted entry sharing that key into a
        single entry keyed under ``new_id`` (keeping the freshest name/addr) and
        drop the stale duplicates. Returns the canonical entry, or ``None`` if
        no trusted peer matches ``fp`` (a genuine stranger — left untrusted)."""
        if not fp or not new_id:
            return None
        dups = [eid for eid in list(self.trusted)
                if _fp_of(self.trusted[eid].get("pubkey", "")) == fp]
        if not dups or dups == [new_id]:
            return self.trusted.get(new_id)        # already canonical / unknown
        entry = self.trusted.get(new_id) or dict(self.trusted[dups[0]])
        for eid in dups:
            p = self.trusted[eid]
            if not entry.get("name") and p.get("name"):
                entry["name"] = p["name"]
            if not entry.get("addr") and p.get("addr"):
                entry["addr"] = p["addr"]
            if eid != new_id:
                self.trusted.pop(eid, None)
                self._peers_online.pop(eid, None)
        self.trusted[new_id] = entry
        self._save_peers()
        return entry

    def _open_frame(self, frame: dict):
        """Decrypt an incoming sync/media frame, healing device_id drift.

        Returns ``(sender_id, peer, cleartext_bytes)`` or ``(None, None, None)``.
        Tries the named ``from`` peer first, then every other trusted key — NaCl
        Box authenticates, so only the real sender's key decrypts. On a match
        under a changed id, migrates the trusted entry (``_adopt_peer_id``) so
        delivery survives a regenerated ``device-id`` without a re-pair."""
        sender = frame.get("from")
        try:
            cipher = bytes.fromhex(frame.get("box", ""))
        except (ValueError, TypeError):
            return None, None, None
        order = ([sender] if sender in self.trusted else []) + \
                [e for e in list(self.trusted) if e != sender]
        for eid in order:
            peer = self.trusted.get(eid)
            if not peer or not peer.get("pubkey"):
                continue
            try:
                clear = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"]))).decrypt(cipher)
            except Exception:
                continue
            if sender and eid != sender:
                peer = self._adopt_peer_id(_fp_of(peer["pubkey"]), sender) or peer
            return (sender or eid), peer, clear
        return None, None, None

    # -- lifecycle -------------------------------------------------------
    def _bind_listener(self) -> socket.socket:
        """Bind + listen on the sync port, retrying briefly.

        On an in-app update the outgoing daemon relaunches the new one while it
        may still hold this port: its IPC socket closes first (so the
        single-instance check passes) but the listening socket lingers until the
        slow zeroconf teardown finishes. A plain SO_REUSEADDR bind then throws
        EADDRINUSE, _make_engine swallows it, and sync comes up dead with the
        pairing UI hidden. Retry across that hand-off, and set SO_REUSEPORT so
        future restarts (both sides having it) can overlap without a gap."""
        last = None
        for attempt in range(16):   # ~5s: covers the outgoing daemon's teardown
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # not on this platform/kernel — SO_REUSEADDR + retry still work
            try:
                s.bind(("0.0.0.0", self.port))
                s.listen(16)
                return s
            except OSError as exc:
                s.close()
                last = exc
                if attempt < 15:
                    time.sleep(0.3)
        raise last

    def start(self) -> None:
        if not sync_available() or self._priv is None:
            return
        self._running = True
        self._server = self._bind_listener()
        threading.Thread(target=self._serve, daemon=True).start()
        self._live_thread = threading.Thread(target=self._liveness_loop, daemon=True)
        self._live_thread.start()
        self._advertise()

    def stop(self) -> None:
        self._running = False
        # Release the listening port *before* the (slow) zeroconf teardown so a
        # relaunching daemon can bind it promptly.
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass

    def restart_network(self) -> None:
        """Re-establish discovery + the listening socket — e.g. after the
        machine wakes from sleep, when mDNS and sockets often go stale."""
        if not sync_available() or self._priv is None:
            return
        try:
            if self._zc is not None:
                self._zc.close()
        except Exception:
            pass
        self._zc = self._browser = None
        try:
            if self._server is not None:
                self._server.close()   # makes the old _serve accept() break out
        except OSError:
            pass
        self._peers_online.clear()
        try:
            self._running = True
            self._server = self._bind_listener()
            threading.Thread(target=self._serve, daemon=True).start()
            self._advertise()
            print("[clippy-sync] network restarted (wake/resume)", file=sys.stderr)
        except Exception as exc:
            print(f"[clippy-sync] restart failed: {exc}", file=sys.stderr)

    def readvertise(self) -> None:
        """Cheap mDNS refresh (no socket teardown) — call periodically so peers
        that dropped off rediscover us, and so a changed local address is
        published instead of the one we happened to have at startup.

        Rebuilding matters: ``_info`` is a snapshot, so re-announcing it after a
        network change just repeats the stale address. Measured — a laptop paired
        on the LAN joined an iPhone hotspot, registered ``172.20.10.8``, came back
        to the LAN, and kept advertising the hotspot address. Peers dutifully
        relearned it (that part is by design) and every send then timed out
        against an unroutable host until the app was restarted.

        Safe if discovery isn't up."""
        if not _HAVE_ZC or self._zc is None or self._info is None:
            return
        old = self._info
        ip = _local_ip()
        # 127.0.0.1 is what _local_ip falls back to with no route; advertising it
        # would be worse than keeping a stale address, so leave it alone.
        changed = (ip != "127.0.0.1"
                   and socket.inet_aton(ip) not in (old.addresses or []))
        if changed:
            self._info = self._service_info(ip)
        try:
            self._zc.update_service(self._info)
        except Exception:
            try:
                self._zc.unregister_service(old)
                self._zc.register_service(self._info)
            except Exception:
                pass
        if changed:
            _log(f"mDNS: local address changed to {ip} — re-advertised")

    # -- discovery (mDNS) ------------------------------------------------
    def _service_info(self, ip: str):
        """The mDNS record we publish for this device. Built in one place so an
        address refresh can't drift from the original registration."""
        return ServiceInfo(
            config.SYNC_SERVICE,
            f"{self.device_id}.{config.SYNC_SERVICE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties={
                "id": self.device_id,
                "name": self.device_name(),
                "fp": self.fingerprint(),
            },
        )

    def _advertise(self) -> None:
        if not _HAVE_ZC:
            return
        self._zc = Zeroconf()
        self._info = self._service_info(_local_ip())
        try:
            self._zc.register_service(self._info)
        except Exception:
            pass
        self._browser = ServiceBrowser(self._zc, config.SYNC_SERVICE, handlers=[self._on_zc])

    def _on_zc(self, zeroconf, service_type, name, state_change):
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=2000)
        except Exception:
            info = None
        if not info:
            return
        props = {k.decode(): (v.decode() if v else "") for k, v in (info.properties or {}).items()}
        pid = props.get("id")
        if not pid or pid == self.device_id:
            return
        fp = props.get("fp")
        addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        ip = addrs[0] if addrs else None
        if not ip:
            return
        from zeroconf import ServiceStateChange
        if state_change is ServiceStateChange.Removed:
            self._peers_online.pop(pid, None)
        else:
            # mDNS is a DISCOVERY HINT ONLY — never a source of trust changes.
            # The id, fingerprint and address in a TXT record are all public and
            # unauthenticated, so a LAN host can advertise a trusted peer's
            # fingerprint with an attacker-chosen id/address. Acting on that here
            # (re-keying the trusted entry, or repointing its stored address)
            # would let that host hijack where encrypted clips are sent — they
            # couldn't decrypt them, but the real peer would stop receiving them.
            # So this only populates the ephemeral _peers_online map, which is
            # re-derived on every discovery and used merely as a candidate
            # address. Trust and stored addresses change only on authenticated
            # paths: pairing (SPAKE2) and _open_frame's post-decrypt adoption.
            self._peers_online[pid] = (ip, info.port, props.get("name", pid))
        if self._on_status:
            self._on_status()

    # -- server ----------------------------------------------------------
    def _serve(self) -> None:
        while self._running and self._server is not None:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                break
            # Bound concurrent handlers. Anyone who can reach the port — no
            # pairing needed — could otherwise open connections without limit,
            # each spawning a thread and able to hold a buffer until its
            # timeout, exhausting memory and threads. A non-blocking acquire
            # means excess connections are dropped immediately rather than
            # queued, so a flood costs nothing; real peers reconnect and retry.
            if not self._conn_slots.acquire(blocking=False):
                _log("conn: dropped — too many concurrent connections")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(_CONN_TIMEOUT)
                # pair_hello carries only tiny fields; a sync/media frame is the
                # encrypted envelope (its authenticity is checked in _open_frame).
                frame = _recv_frame(conn)
                if not frame:
                    return
                kind = frame.get("type")
                if kind == "pair_hello":
                    self._handle_pair_server(conn, frame)
                elif kind == "ping":
                    # Liveness probe. Reply with our id (already public via
                    # mDNS), so a peer can confirm we're reachable. No trust or
                    # clipboard data is exposed.
                    _send_frame(conn, {"type": "pong", "id": self.device_id})
                elif kind == "sync":
                    self._handle_sync(frame)
                elif kind == "media":
                    conn.settimeout(120)     # a large transfer can take a while
                    self._handle_media(conn, frame)
        finally:
            self._conn_slots.release()

    # -- sync transport --------------------------------------------------
    def _handle_sync(self, frame: dict) -> None:
        _sender, _peer, clear = self._open_frame(frame)
        if clear is None:
            return  # not paired / undecryptable -> reject
        try:
            env = json.loads(clear.decode("utf-8"))
        except Exception:
            return
        self.on_receive(env)

    def on_receive(self, env: dict) -> None:
        if env.get("origin") == self.device_id:
            return
        h = env.get("hash")
        if not h or self._seen_has(h):
            return
        if env.get("kind") != "text":
            return  # v0: text only
        text = env.get("text") or ""
        if not text:
            return
        # Record BEFORE writing the clipboard, so the local watch firing on this
        # write is recognised and not re-broadcast (loop prevention).
        self._seen_add(h)
        try:
            storage.add_text(text, "text/plain")
        except Exception:
            pass
        try:
            from . import clipboard
            clipboard.copy_text(text)   # plain text only (v0)
        except Exception:
            pass
        self._notify_received()

    # -- media receive (streamed) ----------------------------------------
    def _handle_media(self, conn, frame) -> None:
        _sender, peer, clear = self._open_frame(frame)
        if clear is None:
            return
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))  # for chunks
            manifest = json.loads(clear.decode("utf-8"))
        except Exception:
            return
        h = manifest.get("hash")
        size = int(manifest.get("size", 0))
        if not h or self._seen_has(h):
            return
        # Clamp to the hard ceiling, not just the user's setting: a peer names
        # the size and we stream that many decrypted bytes to disk, so a value
        # the user never intended (or a peer claiming a huge transfer) must not
        # be honoured beyond the ceiling regardless of the stored preference.
        cap = min(int(settings.get("sync_max_bytes") or 0), config.SYNC_MAX_CEILING)
        if size <= 0 or size > cap:
            return  # over the cap (or empty) -> refuse
        import hashlib as _hl
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="clippy-recv-")
        os.close(fd)
        received, hasher = 0, _hl.sha256()
        try:
            with open(tmp, "wb") as out:
                while received < size:
                    enc = _recv_raw(conn)
                    if not enc:           # None or b"" (end/closed)
                        break
                    chunk = box.decrypt(enc)
                    out.write(chunk)
                    hasher.update(chunk)
                    received += len(chunk)
        except Exception:
            self._safe_unlink(tmp)
            return
        if received != size or hasher.hexdigest() != h:
            self._safe_unlink(tmp)        # incomplete / corrupt
            return
        self._seen_add(h)                 # before inject (loop prevention)
        self._store_and_inject_media(manifest, tmp)

    def _store_and_inject_media(self, manifest, tmp) -> None:
        import os
        import shutil
        from . import clipboard
        kind = manifest.get("kind")
        mime = manifest.get("mime") or "application/octet-stream"
        try:
            if kind == "image":
                data = open(tmp, "rb").read()
                mime = self._verified_image_mime(data, mime, manifest)
                if mime is None:
                    self._safe_unlink(tmp)
                    return
                storage.add_image(data, mime)
                clipboard.copy_image(data, mime)
                self._safe_unlink(tmp)
            else:
                name = _name_with_ext(
                    os.path.basename(manifest.get("name") or "file") or "file", mime)
                # basename() strips path separators, but "." and ".." survive it
                # and would resolve the destination to RECV_DIR itself or its
                # parent. Fall back to a safe name so a peer can't place the file
                # outside RECV_DIR by one component.
                if name in (".", "..") or "/" in name or "\\" in name:
                    name = "file"
                dest = self._unique_path(config.RECV_DIR / name)
                # Final guard: the resolved path must stay inside RECV_DIR.
                recv_root = os.path.realpath(config.RECV_DIR)
                if os.path.commonpath([os.path.realpath(dest), recv_root]) != recv_root:
                    self._safe_unlink(tmp)
                    return
                shutil.move(tmp, dest)
                storage.add_file_from_path(str(dest), name, mime)
                clipboard.copy_file(str(dest))
            self._notify_received()
        except Exception:
            self._safe_unlink(tmp)

    @staticmethod
    def _verified_image_mime(data: bytes, mime: str, manifest) -> Optional[str]:
        """The MIME to file received image bytes under, or None to drop them.

        A peer's manifest label is a claim about bytes we didn't read ourselves,
        and a wrong one poisons history: the bytes are stored *and* pushed onto
        the clipboard, so every consumer that trusts the label fails to decode
        and pastes an empty image. Measured case — macOS puts ``public.tiff`` on
        the pasteboard for most copied images, and the mac backend labels it
        ``image/png`` regardless, so uncompressed TIFF arrived here wearing a PNG
        label. ``capture.py`` already sniffs magic for locally-read clips; this
        closes the same gap on the receive side.

        Conservative in the same way ``_looks_like_image`` is: only a *positively*
        wrong label is acted on, so an image type we have no magic for (webp,
        gif, avif, …) still passes through untouched."""
        from .capture import _looks_like_image, sniff_image_mime
        base = (mime or "").split(";")[0].strip().lower()
        if _looks_like_image(data, base):
            return mime
        name = os.path.basename(manifest.get("name") or "") or "image"
        actual = sniff_image_mime(data)
        if actual:
            _log(f"media: '{name}' claimed {base}, magic says {actual} — relabelled")
            return actual
        _log(f"media: '{name}' claimed {base} but matches no known image magic "
             f"({data[:8].hex(' ')}) — rejected")
        return None

    def _notify_received(self) -> None:
        cb = self._on_received
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    @staticmethod
    def _unique_path(path):
        import os
        path = str(path)
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    @staticmethod
    def _safe_unlink(p):
        import os
        try:
            os.unlink(p)
        except OSError:
            pass

    def broadcast_id(self, entry_id) -> None:
        """Broadcast a specific stored item by id (the one just captured)."""
        try:
            entry = storage.get(int(entry_id))
        except Exception:
            return
        if entry is not None:
            self._broadcast_entry(entry)

    def broadcast_latest(self) -> None:
        """Broadcast the most-recently-created item. Note: list_entries is
        pinned-first, so prefer broadcast_id(); this is a fallback."""
        try:
            entries = sorted(storage.list_entries(limit=50),
                             key=lambda e: e.created_at, reverse=True)
        except Exception:
            return
        if entries:
            self._broadcast_entry(entries[0])

    def _peer_addrs(self, pid, peer):
        """Ordered, de-duped candidate (ip, port) for a peer: the live mDNS
        address first, then the last-known stored address. Trying *both* (not
        just one) is what survives a stale mDNS record or a dual-homed peer
        whose advertised IP is momentarily unroutable."""
        addrs = []
        online = self._peers_online.get(pid)
        if online:
            addrs.append((online[0], online[1]))
        if peer.get("addr"):
            a = (peer["addr"], config.SYNC_PORT)
            if a not in addrs:
                addrs.append(a)
        return addrs

    def _broadcast_entry(self, entry) -> None:
        h = getattr(entry, "hash", None)
        if not h or self._seen_has(h):
            return  # just received/sent this — don't echo
        kind = getattr(entry, "kind", "text")
        if kind == "text":
            text = getattr(entry, "text", None)
            if not text:
                return
            env = {"v": PROTO, "origin": self.device_id, "ts": int(time.time()),
                   "hash": h, "kind": "text", "mime": "text/plain", "text": text}
            payload = json.dumps(env).encode("utf-8")
            for pid, peer in list(self.trusted.items()):
                addrs = self._peer_addrs(pid, peer)
                if addrs:
                    threading.Thread(target=self._deliver_text,
                                     args=(addrs, peer, h, payload), daemon=True).start()
            return
        # media (image / file): stream the on-disk blob, capped + integrity-checked.
        import os
        blob = getattr(entry, "image_path", None)
        if not blob or not os.path.exists(blob):
            return
        size = getattr(entry, "size", 0) or os.path.getsize(blob)
        if size > settings.get("sync_max_bytes"):
            print(f"[clippy-sync] '{getattr(entry,'filename',None) or kind}' "
                  f"({size} B) exceeds the sync size limit — not sent.")
            return
        peers = [(self._peer_addrs(pid, peer), peer)
                 for pid, peer in list(self.trusted.items())]
        peers = [(a, p) for a, p in peers if a]
        if not peers:
            return  # nothing paired/reachable -> no transfer, no progress bar
        mime = getattr(entry, "mime", None) or "application/octet-stream"
        name = _name_with_ext(getattr(entry, "filename", None) or os.path.basename(blob), mime)
        manifest = {"v": PROTO, "origin": self.device_id, "hash": h, "kind": kind,
                    "mime": mime, "name": name, "size": size}
        for addrs, peer in peers:
            threading.Thread(target=self._send_media_to,
                             args=(addrs, peer, blob, manifest, h), daemon=True).start()

    def _deliver_text(self, addrs, peer, h, payload: bytes) -> None:
        """Send one text payload to a peer, retrying across its candidate
        addresses; mark the hash 'seen' only once a send actually succeeds so a
        transient failure doesn't suppress a later re-copy of the same text."""
        name = peer.get("name", "peer")
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            frame = {"type": "sync", "from": self.device_id,
                     "box": bytes(box.encrypt(payload)).hex()}
        except Exception as exc:
            _log(f"text: encrypt for {name} failed: {exc!r}")
            return
        last = None
        for attempt in range(_SEND_ATTEMPTS):
            for ip, port in addrs:
                try:
                    with socket.create_connection((ip, port), timeout=_CONN_TIMEOUT) as s:
                        _send_frame(s, frame)
                    _log(f"text: delivered to {name} via {ip}:{port} "
                         f"(attempt {attempt + 1})")
                    self._seen_add(h)
                    return
                except Exception as exc:
                    last = exc
            if attempt + 1 < _SEND_ATTEMPTS:
                time.sleep(_SEND_BACKOFF * (attempt + 1))
        _log(f"text: send to {name} FAILED after {_SEND_ATTEMPTS}x over "
             f"{addrs}: {last!r}")

    def _send_media_to(self, addrs, peer, blob, manifest, h) -> None:
        """Stream an on-disk blob to one peer (trying each candidate address,
        first success wins), with progress; mark 'seen' on success."""
        total = manifest["size"]
        name = manifest["name"]
        pname = peer.get("name", "peer")
        show = (self._on_progress is not None
                and total > settings.get("progress_min_bytes"))
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            mframe = {"type": "media", "from": self.device_id,
                      "box": bytes(box.encrypt(
                          json.dumps(manifest).encode("utf-8"))).hex()}
        except Exception as exc:
            _log(f"media: encrypt for {pname} failed: {exc!r}")
            return
        last = None
        for ip, port in addrs:
            sent = 0
            try:
                with socket.create_connection((ip, port), timeout=_CONN_TIMEOUT) as s:
                    s.settimeout(120)
                    _send_frame(s, mframe)
                    with open(blob, "rb") as f:
                        while True:
                            chunk = f.read(config.SYNC_CHUNK)
                            if not chunk:
                                break
                            _send_raw(s, bytes(box.encrypt(chunk)))
                            sent += len(chunk)
                            if show:
                                self._on_progress(name, sent, total, False)
                    _send_raw(s, b"")          # end-of-stream marker
                if show:
                    self._on_progress(name, total, total, True)
                _log(f"media: '{name}' delivered to {pname} via {ip}:{port}")
                self._seen_add(h)
                return
            except Exception as exc:
                last = exc
                if show:
                    self._on_progress(name, sent, total, True)  # close the bar
        _log(f"media: '{name}' send to {pname} FAILED over {addrs}: {last!r}")

    # -- seen-hash LRU ---------------------------------------------------
    def _seen_has(self, h: str) -> bool:
        with self._lock:
            ts = self._seen.get(h)
            return ts is not None and (time.time() - ts) < _SEEN_TTL

    def _seen_add(self, h: str) -> None:
        with self._lock:
            self._seen[h] = time.time()
            while len(self._seen) > _SEEN_MAX:
                self._seen.popitem(last=False)

    def _clear_stale(self, pid: str, pubkey: str) -> None:
        """Drop a stale (pre-fix) record once its device is re-paired — matched
        by id or by identity key, since a re-pair may arrive under a new id."""
        for sid in [s for s, e in self.stale_peers.items()
                    if s == pid or e.get("pubkey") == pubkey]:
            self.stale_peers.pop(sid, None)

    # -- liveness --------------------------------------------------------
    def _liveness_loop(self) -> None:
        """Periodically confirm each trusted peer is actually reachable.

        Records when each last answered so ``status`` can report real
        reachability (green when a peer replied within _LIVENESS_WINDOW) and the
        UI can show when the check last ran, instead of trusting mDNS presence
        which lags a peer that dropped off without a goodbye."""
        while self._running:
            for pid, peer in list(self.trusted.items()):
                if self._ping_peer(pid, peer):
                    self._last_seen[pid] = time.time()
            self._last_check = time.time()
            if self._on_status:
                try:
                    self._on_status()
                except Exception:
                    pass
            for _ in range(_LIVENESS_INTERVAL * 2):
                if not self._running:
                    return
                time.sleep(0.5)

    def _ping_peer(self, pid: str, peer: dict) -> bool:
        """True if the peer answered a ping on any of its candidate addresses."""
        for ip, port in self._peer_addrs(pid, peer):
            try:
                with socket.create_connection((ip, port), timeout=2) as s:
                    s.settimeout(2)
                    _send_frame(s, {"type": "ping", "id": self.device_id})
                    reply = _recv_frame(s, limit=_MAX_PAIR_MSG)
                if reply and reply.get("type") == "pong":
                    return True
            except OSError:
                continue
        return False

    # -- pairing ---------------------------------------------------------
    #
    # Both devices run SPAKE2 keyed by the shown code. SPAKE2 turns the shared
    # low-entropy code into a shared high-entropy session key WITHOUT either side
    # transmitting the code, a hash of it, or anything an eavesdropper (or the
    # peer we're talking to) can offline-crack. Each side then proves it derived
    # the same key by sending a MAC over a transcript that binds the pairing
    # version, both SPAKE2 messages and BOTH long-term X25519 identity pubkeys,
    # so a successful pairing authenticates the keys the data plane will use.
    #
    # Why this is not the old scheme: there is nothing to echo. The confirmation
    # tag is keyed by a session key the attacker cannot compute without the code,
    # and a wrong code yields a different key on each side, so the MACs simply
    # don't match. Online guessing is the only attack, and it is bounded by
    # _PAIR_MAX_ATTEMPTS + the _PAIR_TIMEOUT window.
    @staticmethod
    def _pair_key_confirm(session_key: bytes, msg_a: bytes, msg_b: bytes,
                          pk_a: str, pk_b: str, who: bytes) -> str:
        """One side's key-confirmation MAC. ``who`` (b"A"/b"B") makes the two
        sides' tags distinct, so neither can be replayed as the other's."""
        transcript = (_PAIR_TRANSCRIPT + b"|" + msg_a + b"|" + msg_b + b"|"
                      + bytes.fromhex(pk_a) + b"|" + bytes.fromhex(pk_b))
        return hmac.new(session_key, transcript + b"|" + who,
                        hashlib.sha256).hexdigest()

    def enter_pairing(self) -> str:
        """Show-a-code mode (device A). Returns the 6-digit code to display."""
        code = "%06d" % (struct.unpack(">I", os.urandom(4))[0] % 1_000_000)
        self._pairing = {"code": code, "deadline": time.time() + _PAIR_TIMEOUT,
                         "attempts": 0}
        return code

    def _pairing_active(self) -> Optional[str]:
        # Handlers run one per connection thread, so the window check and the
        # attempt counter are shared mutable state — guard them, or concurrent
        # guesses race the counter and a few slip past the cap.
        with self._pair_lock:
            p = self._pairing
            if p and time.time() < p["deadline"] and p["attempts"] < _PAIR_MAX_ATTEMPTS:
                return p["code"]
            self._pairing = None
            return None

    def _pair_fail(self, conn, reason: str) -> None:
        """Count a failed attempt and burn the code once the cap is hit, so the
        1e6-code space can't be walked during one 120s window."""
        with self._pair_lock:
            if self._pairing is not None:
                self._pairing["attempts"] = self._pairing.get("attempts", 0) + 1
                if self._pairing["attempts"] >= _PAIR_MAX_ATTEMPTS:
                    self._pairing = None
        try:
            _send_frame(conn, {"type": "pair_err", "reason": reason})
        except OSError:
            pass

    def _handle_pair_server(self, conn, frame) -> None:
        code = self._pairing_active()
        if not code:
            _send_frame(conn, {"type": "pair_err", "reason": "not pairing"})
            return
        if frame.get("proto") != _PAIR_PROTO:
            _send_frame(conn, {"type": "pair_err",
                               "reason": "version mismatch — update Clippy on both devices"})
            return
        pk_b = frame.get("pubkey", "")
        id_b = frame.get("id", "")
        name_b = frame.get("name", id_b)
        msg_b = _unhex(frame.get("spake", ""))
        if not pk_b or not id_b or msg_b is None:
            self._pair_fail(conn, "bad request")
            return
        try:
            bytes.fromhex(pk_b)
        except ValueError:
            self._pair_fail(conn, "bad request")
            return
        # SPAKE2 side A: derive the session key from our message + the peer's.
        spake = SPAKE2_A(code.encode("utf-8"))
        msg_a = spake.start()
        try:
            session_key = spake.finish(msg_b)
        except Exception:
            self._pair_fail(conn, "code mismatch")
            return
        our_tag = self._pair_key_confirm(session_key, msg_a, msg_b,
                                         self.pubkey_hex, pk_b, b"A")
        peer_expected = self._pair_key_confirm(session_key, msg_a, msg_b,
                                               self.pubkey_hex, pk_b, b"B")
        _send_frame(conn, {"type": "pair_ack", "id": self.device_id,
                           "name": self.device_name(), "pubkey": self.pubkey_hex,
                           "proto": _PAIR_PROTO, "spake": msg_a.hex(),
                           "confirm": our_tag})
        reply = _recv_frame(conn, limit=_MAX_PAIR_MSG)
        # Any outcome that isn't a valid confirm counts as a failed attempt, so
        # the code is burned after _PAIR_MAX_ATTEMPTS. This has to cover a
        # missing/timed-out reply too, not just a mismatched one: with mutual
        # auth a wrong-code peer detects OUR tag doesn't match and hangs up
        # before sending its own, and a guessing attacker likewise never
        # produces a valid confirm — both must be counted, or the cap is
        # walk-around-able. The comparison is constant-time and against the
        # value WE computed for the peer (the "B" tag), never one we
        # transmitted, so there is nothing for an attacker to echo back.
        if (not reply or reply.get("type") != "pair_confirm"
                or not hmac.compare_digest(reply.get("confirm", ""), peer_expected)):
            self._pair_fail(conn, "code mismatch")
            return
        try:
            peer_ip = conn.getpeername()[0]
        except OSError:
            peer_ip = None
        # Remember the peer's address so we can sync to it even if mDNS never
        # discovers it (multicast-blocked networks / multi-homed hosts). Tag the
        # entry with the pairing protocol version so a future upgrade can tell
        # securely-paired trust from the old kind.
        self.trusted[id_b] = {"name": name_b, "pubkey": pk_b, "addr": peer_ip,
                              "proto": _PAIR_PROTO}
        self._clear_stale(id_b, pk_b)
        self._save_peers()
        with self._pair_lock:
            self._pairing = None
        _send_frame(conn, {"type": "paired", "name": self.device_name()})
        if self._on_status:
            self._on_status()

    def join_pairing(self, code: str, host: Optional[str] = None) -> dict:
        """Enter-a-code mode (device B). With ``host`` set, connect straight to
        that IP (no mDNS needed); otherwise try the mDNS-discovered peers."""
        code = code.strip()
        if host:
            return self._pair_client(host, config.SYNC_PORT, code)
        peers = list(self._peers_online.items())
        if not peers:
            return {"ok": False,
                    "error": "no devices found on the LAN (mDNS may be blocked — "
                             "retry with the other device's IP: clippy pair CODE IP)"}
        for pid, (ip, port, name) in peers:
            res = self._pair_client(ip, port, code)
            if res.get("ok"):
                return res
        return {"ok": False, "error": "no device in pairing mode matched the code"}

    def _pair_client(self, ip, port, code: str) -> dict:
        try:
            with socket.create_connection((ip, port), timeout=_CONN_TIMEOUT) as s:
                s.settimeout(_CONN_TIMEOUT)
                # SPAKE2 side B: send our message, receive theirs, derive the key.
                spake = SPAKE2_B(code.encode("utf-8"))
                msg_b = spake.start()
                _send_frame(s, {"type": "pair_hello", "id": self.device_id,
                                "name": self.device_name(), "pubkey": self.pubkey_hex,
                                "proto": _PAIR_PROTO, "spake": msg_b.hex()})
                ack = _recv_frame(s, limit=_MAX_PAIR_MSG)
                if not ack or ack.get("type") != "pair_ack":
                    return {"ok": False, "error": (ack or {}).get("reason", "no ack")}
                pk_a = ack.get("pubkey", "")
                msg_a = _unhex(ack.get("spake", ""))
                if not pk_a or msg_a is None:
                    return {"ok": False, "error": "bad response"}
                try:
                    bytes.fromhex(pk_a)
                except ValueError:
                    return {"ok": False, "error": "bad response"}
                try:
                    session_key = spake.finish(msg_a)
                except Exception:
                    return {"ok": False, "error": "code mismatch"}
                # Verify the peer proved the SAME code (its "A" tag), then send
                # our own "B" tag. A wrong code makes these keys differ, so the
                # peer's tag won't match and ours won't satisfy the peer either.
                peer_expected = self._pair_key_confirm(session_key, msg_a, msg_b,
                                                       pk_a, self.pubkey_hex, b"A")
                if not hmac.compare_digest(ack.get("confirm", ""), peer_expected):
                    return {"ok": False, "error": "code mismatch"}
                our_tag = self._pair_key_confirm(session_key, msg_a, msg_b,
                                                 pk_a, self.pubkey_hex, b"B")
                _send_frame(s, {"type": "pair_confirm", "confirm": our_tag})
                done = _recv_frame(s, limit=_MAX_PAIR_MSG)
                if not done or done.get("type") != "paired":
                    return {"ok": False, "error": (done or {}).get("reason", "peer rejected")}
                self.trusted[ack["id"]] = {"name": ack.get("name", ack["id"]),
                                           "pubkey": pk_a, "addr": ip,
                                           "proto": _PAIR_PROTO}
                self._clear_stale(ack["id"], pk_a)
                self._save_peers()
                if self._on_status:
                    self._on_status()
                return {"ok": True, "name": ack.get("name", ack["id"])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -- status ----------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        peers = []
        for pid, info in self.trusted.items():
            seen = self._last_seen.get(pid, 0)
            peers.append({
                "id": pid, "name": info.get("name", pid),
                # Online = answered a liveness ping within the window. Falls back
                # to mDNS presence before the first sweep completes, so a freshly
                # started peer isn't shown offline for the first interval.
                "online": (now - seen) < _LIVENESS_WINDOW
                          or (self._last_check == 0 and pid in self._peers_online),
                "last_seen": seen or None,
            })
        return {"device": self.device_name(), "id": self.device_id,
                "fingerprint": self.fingerprint(), "peers": peers,
                "discovered": len(self._peers_online),
                "last_check": self._last_check or None,
                # Devices paired before the security update, awaiting re-pair.
                "stale": [{"id": i, "name": e.get("name", i)}
                          for i, e in self.stale_peers.items()]}

    def forget_stale(self, pid: str) -> bool:
        """Drop a stale peer the user chooses not to re-pair (a normal unpair)."""
        if self.stale_peers.pop(pid, None) is not None:
            self._save_peers()
            if self._on_status:
                self._on_status()
            return True
        return False
