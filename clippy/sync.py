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


def import_error() -> str:
    """Why sync is unavailable (the real ImportError), for diagnostics."""
    return _IMPORT_ERROR.strip()

PROTO = 1
_PAIR_TRANSCRIPT = b"clippy-pair-v1"
_PAIR_TIMEOUT = 120          # seconds a shown code stays valid
_CONN_TIMEOUT = 5
_SEEN_MAX = 256


def sync_available() -> bool:
    return _HAVE_NACL and _HAVE_ZC


# --- framing ---------------------------------------------------------------
def _send_frame(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_frame(sock: socket.socket) -> Optional[dict]:
    hdr = _recv_exact(sock, 4)
    if not hdr:
        return None
    (length,) = struct.unpack(">I", hdr)
    if length <= 0 or length > 64 * 1024 * 1024:
        return None
    body = _recv_exact(sock, length)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return None


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


class SyncEngine:
    def __init__(self, on_status: Optional[Callable[[], None]] = None,
                 port: Optional[int] = None, state_dir=None,
                 on_progress: Optional[Callable] = None):
        self._on_status = on_status
        self._on_progress = on_progress   # (name, sent, total, done) for big sends
        self._lock = threading.Lock()
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._server: Optional[socket.socket] = None
        self._running = False
        self._zc = None
        self._info = None
        self._browser = None
        self._peers_online: Dict[str, tuple] = {}   # id -> (ip, port, name)
        self._pairing = None                          # dict while in pairing mode

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
        self.trusted = self._load_peers()             # id -> {name, pubkey}

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

    def _load_peers(self) -> Dict[str, dict]:
        try:
            return json.loads(self._peers_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_peers(self) -> None:
        self._peers_path.write_text(json.dumps(self.trusted, indent=2))
        try:
            os.chmod(self._peers_path, 0o600)
        except OSError:
            pass

    def fingerprint(self) -> str:
        return hashlib.sha256(bytes.fromhex(self.pubkey_hex)).hexdigest()[:16] if self.pubkey_hex else ""

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not sync_available() or self._priv is None:
            return
        self._running = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("0.0.0.0", self.port))
        self._server.listen(16)
        threading.Thread(target=self._serve, daemon=True).start()
        self._advertise()

    def stop(self) -> None:
        self._running = False
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
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
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("0.0.0.0", self.port))
            self._server.listen(16)
            threading.Thread(target=self._serve, daemon=True).start()
            self._advertise()
            print("[clippy-sync] network restarted (wake/resume)", file=sys.stderr)
        except Exception as exc:
            print(f"[clippy-sync] restart failed: {exc}", file=sys.stderr)

    def readvertise(self) -> None:
        """Cheap mDNS refresh (no socket teardown) — call periodically so peers
        that dropped off rediscover us. Safe if discovery isn't up."""
        if not _HAVE_ZC or self._zc is None or self._info is None:
            return
        try:
            self._zc.update_service(self._info)
        except Exception:
            try:
                self._zc.unregister_service(self._info)
                self._zc.register_service(self._info)
            except Exception:
                pass

    # -- discovery (mDNS) ------------------------------------------------
    def _advertise(self) -> None:
        if not _HAVE_ZC:
            return
        ip = _local_ip()
        self._zc = Zeroconf()
        self._info = ServiceInfo(
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
        addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        ip = addrs[0] if addrs else None
        if not ip:
            return
        from zeroconf import ServiceStateChange
        if state_change is ServiceStateChange.Removed:
            self._peers_online.pop(pid, None)
        else:
            self._peers_online[pid] = (ip, info.port, props.get("name", pid))
            # Keep a paired peer's last-known address fresh for the mDNS-free path.
            if pid in self.trusted and self.trusted[pid].get("addr") != ip:
                self.trusted[pid]["addr"] = ip
                self._save_peers()
        if self._on_status:
            self._on_status()

    # -- server ----------------------------------------------------------
    def _serve(self) -> None:
        while self._running and self._server is not None:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(_CONN_TIMEOUT)
            frame = _recv_frame(conn)
            if not frame:
                return
            kind = frame.get("type")
            if kind == "pair_hello":
                self._handle_pair_server(conn, frame)
            elif kind == "sync":
                self._handle_sync(frame)
            elif kind == "media":
                conn.settimeout(120)     # a large transfer can take a while
                self._handle_media(conn, frame)

    # -- sync transport --------------------------------------------------
    def _handle_sync(self, frame: dict) -> None:
        sender = frame.get("from")
        peer = self.trusted.get(sender)
        if not peer:
            return  # not paired -> reject
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            clear = box.decrypt(bytes.fromhex(frame["box"]))
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

    # -- media receive (streamed) ----------------------------------------
    def _handle_media(self, conn, frame) -> None:
        sender = frame.get("from")
        peer = self.trusted.get(sender)
        if not peer:
            return
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            manifest = json.loads(box.decrypt(bytes.fromhex(frame["box"])).decode("utf-8"))
        except Exception:
            return
        h = manifest.get("hash")
        size = int(manifest.get("size", 0))
        if not h or self._seen_has(h):
            return
        if size <= 0 or size > settings.get("sync_max_bytes"):
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
        name = _name_with_ext(os.path.basename(manifest.get("name") or "file") or "file", mime)
        try:
            if kind == "image":
                data = open(tmp, "rb").read()
                storage.add_image(data, mime)
                clipboard.copy_image(data, mime)
                self._safe_unlink(tmp)
            else:
                dest = self._unique_path(config.RECV_DIR / name)
                shutil.move(tmp, dest)
                storage.add_file_from_path(str(dest), name, mime)
                clipboard.copy_file(str(dest))
        except Exception:
            self._safe_unlink(tmp)

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

    def _peer_targets(self):
        """Yield (peer, ip, port) for each paired peer we can reach (mDNS first,
        stored address as the mDNS-free fallback)."""
        for pid, peer in list(self.trusted.items()):
            online = self._peers_online.get(pid)
            if online:
                yield peer, online[0], online[1]
            elif peer.get("addr"):
                yield peer, peer["addr"], config.SYNC_PORT

    def _broadcast_entry(self, entry) -> None:
        h = getattr(entry, "hash", None)
        if not h or self._seen_has(h):
            return  # just received/sent this — don't echo
        kind = getattr(entry, "kind", "text")
        if kind == "text":
            text = getattr(entry, "text", None)
            if not text:
                return
            self._seen_add(h)
            env = {"v": PROTO, "origin": self.device_id, "ts": int(time.time()),
                   "hash": h, "kind": "text", "mime": "text/plain", "text": text}
            payload = json.dumps(env).encode("utf-8")
            for peer, ip, port in self._peer_targets():
                threading.Thread(target=self._send_to, args=(ip, port, peer, payload),
                                 daemon=True).start()
            return
        # media (image / file): stream the on-disk blob, capped + integrity-checked.
        import os
        blob = getattr(entry, "image_path", None)
        if not blob or not os.path.exists(blob):
            return
        size = getattr(entry, "size", 0) or os.path.getsize(blob)
        if size > settings.get("sync_max_bytes"):
            try:
                from . import setup  # notify helper lives outside; fall back to print
            except Exception:
                pass
            print(f"[clippy-sync] '{getattr(entry,'filename',None) or kind}' "
                  f"({size} B) exceeds the sync size limit — not sent.")
            return
        targets = list(self._peer_targets())
        if not targets:
            return  # nothing paired/online -> no transfer, no progress bar
        self._seen_add(h)
        mime = getattr(entry, "mime", None) or "application/octet-stream"
        name = _name_with_ext(getattr(entry, "filename", None) or os.path.basename(blob), mime)
        manifest = {"v": PROTO, "origin": self.device_id, "hash": h, "kind": kind,
                    "mime": mime, "name": name, "size": size}
        for peer, ip, port in targets:
            threading.Thread(target=self._send_media_to,
                             args=(ip, port, peer, blob, manifest), daemon=True).start()

    def _send_to(self, ip, port, peer, payload: bytes) -> None:
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            enc = box.encrypt(payload)
            with socket.create_connection((ip, port), timeout=_CONN_TIMEOUT) as s:
                _send_frame(s, {"type": "sync", "from": self.device_id, "box": bytes(enc).hex()})
        except Exception:
            pass

    def _send_media_to(self, ip, port, peer, blob, manifest) -> None:
        """Stream an on-disk blob to one peer as encrypted chunks, with progress."""
        total = manifest["size"]
        name = manifest["name"]
        show = (self._on_progress is not None
                and total > settings.get("progress_min_bytes"))
        sent = 0
        try:
            box = Box(self._priv, PublicKey(bytes.fromhex(peer["pubkey"])))
            with socket.create_connection((ip, port), timeout=_CONN_TIMEOUT) as s:
                s.settimeout(120)
                _send_frame(s, {"type": "media", "from": self.device_id,
                                "box": bytes(box.encrypt(
                                    json.dumps(manifest).encode("utf-8"))).hex()})
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
        except Exception:
            if show:
                self._on_progress(name, sent, total, True)  # close the bar on failure

    # -- seen-hash LRU ---------------------------------------------------
    def _seen_has(self, h: str) -> bool:
        with self._lock:
            return h in self._seen

    def _seen_add(self, h: str) -> None:
        with self._lock:
            self._seen[h] = time.time()
            while len(self._seen) > _SEEN_MAX:
                self._seen.popitem(last=False)

    # -- pairing ---------------------------------------------------------
    def _pair_confirm(self, code: str, pk_a: str, pk_b: str) -> str:
        lo, hi = sorted([pk_a, pk_b])
        msg = _PAIR_TRANSCRIPT + bytes.fromhex(lo) + bytes.fromhex(hi)
        return hmac.new(code.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    def enter_pairing(self) -> str:
        """Show-a-code mode (device A). Returns the 6-digit code to display."""
        code = "%06d" % (struct.unpack(">I", os.urandom(4))[0] % 1_000_000)
        self._pairing = {"code": code, "deadline": time.time() + _PAIR_TIMEOUT}
        return code

    def _pairing_active(self) -> Optional[str]:
        p = self._pairing
        if p and time.time() < p["deadline"]:
            return p["code"]
        self._pairing = None
        return None

    def _handle_pair_server(self, conn, frame) -> None:
        code = self._pairing_active()
        if not code:
            _send_frame(conn, {"type": "pair_err", "reason": "not pairing"})
            return
        pk_b = frame.get("pubkey", "")
        id_b = frame.get("id", "")
        name_b = frame.get("name", id_b)
        if not pk_b or not id_b:
            return
        confirm = self._pair_confirm(code, self.pubkey_hex, pk_b)
        _send_frame(conn, {"type": "pair_ack", "id": self.device_id,
                           "name": self.device_name(), "pubkey": self.pubkey_hex,
                           "confirm": confirm})
        reply = _recv_frame(conn)
        if not reply or reply.get("type") != "pair_confirm":
            return
        if not hmac.compare_digest(reply.get("confirm", ""), confirm):
            _send_frame(conn, {"type": "pair_err", "reason": "code mismatch"})
            return
        try:
            peer_ip = conn.getpeername()[0]
        except OSError:
            peer_ip = None
        # Remember the peer's address so we can sync to it even if mDNS never
        # discovers it (multicast-blocked networks / multi-homed hosts).
        self.trusted[id_b] = {"name": name_b, "pubkey": pk_b, "addr": peer_ip}
        self._save_peers()
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
                _send_frame(s, {"type": "pair_hello", "id": self.device_id,
                                "name": self.device_name(), "pubkey": self.pubkey_hex})
                ack = _recv_frame(s)
                if not ack or ack.get("type") != "pair_ack":
                    return {"ok": False, "error": (ack or {}).get("reason", "no ack")}
                pk_a = ack.get("pubkey", "")
                expect = self._pair_confirm(code, pk_a, self.pubkey_hex)
                if not hmac.compare_digest(ack.get("confirm", ""), expect):
                    return {"ok": False, "error": "code mismatch"}
                _send_frame(s, {"type": "pair_confirm", "confirm": expect})
                done = _recv_frame(s)
                if not done or done.get("type") != "paired":
                    return {"ok": False, "error": "peer rejected"}
                self.trusted[ack["id"]] = {"name": ack.get("name", ack["id"]),
                                           "pubkey": pk_a, "addr": ip}
                self._save_peers()
                if self._on_status:
                    self._on_status()
                return {"ok": True, "name": ack.get("name", ack["id"])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -- status ----------------------------------------------------------
    def status(self) -> dict:
        peers = []
        for pid, info in self.trusted.items():
            peers.append({
                "id": pid, "name": info.get("name", pid),
                "online": pid in self._peers_online,
            })
        return {"device": self.device_name(), "id": self.device_id,
                "fingerprint": self.fingerprint(), "peers": peers,
                "discovered": len(self._peers_online)}
