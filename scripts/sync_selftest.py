#!/usr/bin/env python3
"""Single-machine self-test for clippy LAN sync.

Spins up two in-process SyncEngine instances with separate identities/ports and
exercises the whole core over real sockets — without touching your real
clipboard (storage/clipboard writes are stubbed and just recorded). Proves:
discovery, code pairing, wrong-code rejection, encrypted delivery, loop
prevention, and unpaired-sender rejection.

Run:  PYTHONPATH=. python3 scripts/sync_selftest.py
Needs: python3-nacl + python3-zeroconf  (or: pip install --user pynacl zeroconf)
"""
import hashlib
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, ".")
from clippy import sync          # noqa: E402
import clippy.storage as st      # noqa: E402
import clippy.clipboard as cb    # noqa: E402

if not sync.sync_available():
    print("FAIL: pynacl/zeroconf not installed "
          "(sudo apt install python3-nacl python3-zeroconf)")
    raise SystemExit(1)

# Keep the run out of the real <data>/sync.log — this is a test, not traffic.
sync._SYNC_LOG = pathlib.Path(tempfile.mkdtemp()) / "sync.log"

A = sync.SyncEngine(port=48001, state_dir=tempfile.mkdtemp())
B = sync.SyncEngine(port=48002, state_dir=tempfile.mkdtemp())
A.start(); B.start()
print(f"1. two devices up: A={A.device_id[:8]} B={B.device_id[:8]}")

# mDNS discovery (best-effort; some networks block multicast)
seen = False
for _ in range(16):
    time.sleep(0.5)
    if B.device_id in A._peers_online and A.device_id in B._peers_online:
        seen = True
        break
print(f"2. mDNS discovery: {'peers found each other' if seen else 'not seen (mDNS blocked?)'}")

# Pair using the shown code (inject the addr in case mDNS was blocked)
B._peers_online.setdefault(A.device_id, ("127.0.0.1", 48001, "A"))
code = A.enter_pairing()
res = B.join_pairing(code)
assert res.get("ok") and A.device_id in B.trusted and B.device_id in A.trusted, res
print(f"3. paired mutually with code {code}")

A.enter_pairing()
assert not B._pair_client("127.0.0.1", 48001, "000000").get("ok")
print("4. wrong code rejected")

# 4a. The v1 auth bypass: an attacker who never knew the code connects, reads
# the server's confirm out of pair_ack, and echoes it straight back. Under v1
# this pairing succeeded (the server compared the reply against the value it had
# just transmitted). Under v2 (SPAKE2) there is nothing to echo — the tag is
# keyed by a session key the attacker can't derive without the code — so this
# must fail AND must not leave the attacker in A.trusted.
import socket as _sock
A.enter_pairing()
attacker_id = "attacker-" + "0" * 8
before = dict(A.trusted)
try:
    with _sock.create_connection(("127.0.0.1", 48001), timeout=5) as s:
        s.settimeout(5)
        # a valid-looking SPAKE2 message so the server gets past parsing
        from spake2 import SPAKE2_B as _SB
        sp = _SB(b"999999")           # attacker guesses/uses any code
        sync._send_frame(s, {"type": "pair_hello", "id": attacker_id,
                             "name": "attacker", "pubkey": B.pubkey_hex,
                             "proto": sync._PAIR_PROTO, "spake": sp.start().hex()})
        ack = sync._recv_frame(s)
        # echo the server's own confirm back — the v1 bypass
        sync._send_frame(s, {"type": "pair_confirm",
                             "confirm": (ack or {}).get("confirm", "")})
        done = sync._recv_frame(s)
        assert not done or done.get("type") != "paired", "ECHO BYPASS SUCCEEDED — vuln!"
except AssertionError:
    raise
except Exception:
    pass  # a dropped connection is a fine (rejecting) outcome
assert attacker_id not in A.trusted and A.trusted == before, \
    "attacker was trusted despite not knowing the code"
print("4a. pairing echo/replay bypass rejected (SPAKE2)")

# 4b. Attempt cap: a burst of wrong-code tries burns the code so the 1e6 space
# can't be walked inside one window. The server counts each failure on its own
# handler thread, so poll for the window to close rather than assume the count
# landed the instant the client call returned.
A.enter_pairing()
for _ in range(sync._PAIR_MAX_ATTEMPTS):
    B._pair_client("127.0.0.1", 48001, "111111")
deadline = time.time() + 3
while A._pairing is not None and time.time() < deadline:
    time.sleep(0.05)
assert A._pairing is None, "pairing window not closed after the attempt cap"
# And the burned code can no longer be used, even with the RIGHT code.
assert not B._pair_client("127.0.0.1", 48001, "111111").get("ok")
print("4b. attempt cap burns the code after repeated wrong guesses")

# Encrypted broadcast B -> A, with the clipboard/storage stubbed
got = []
st.add_text = lambda text, mime="text/plain", html=None: got.append(text) or 1
cb.copy_text = lambda text: got.append("COPY:" + text)
cb.copy_html = lambda html: got.append("COPYHTML")

_TXT = "secret message ✨"
class E:
    kind = "text"; text = _TXT; mime = "text/plain"; html = None
    hash = hashlib.sha256(_TXT.encode()).hexdigest()

B._broadcast_entry(E())
time.sleep(1.2)
assert "COPY:" + _TXT in got, got
print("5. encrypted text delivered + injected")

n = len(got)
A.on_receive({"v": 1, "origin": B.device_id, "hash": E.hash, "kind": "text",
              "mime": "text/plain", "text": _TXT})
assert len(got) == n, "echo not dropped"
print("6. loop prevention drops the echo")

n = len(got)
A._handle_sync({"from": "deadbeef", "box": "00"})
assert len(got) == n
print("7. unpaired sender rejected")

# ---- media (image + arbitrary file), streamed + integrity-checked ----
import os
import tempfile
import clippy.settings as cs
import clippy.storage as cst

_caps = {"sync_max_bytes": 50 * 1024 * 1024, "progress_min_bytes": 1 * 1024 * 1024}
_realget = cs.get
cs.get = lambda k: _caps.get(k, _realget(k))

A._peers_online[B.device_id] = ("127.0.0.1", 48002, "B")  # simulate mDNS (B's port)

recv = {}
cst.add_image = lambda data, mime="image/png": (recv.__setitem__("img", 1), 1)[1]
cst.add_file_from_path = lambda src, name, mime="application/octet-stream": (recv.__setitem__("f", 1), 1)[1]
cb.copy_image = lambda data, mime: recv.__setitem__("copyimg", hashlib.sha256(data).hexdigest())
cb.copy_file = lambda path: recv.__setitem__("copyfile", path)
prog = []
A._on_progress = lambda name, sent, total, done: prog.append((sent, total, done))

def _blob(size, magic=b""):
    # `magic` leads the payload so a blob announced as a given image type still
    # looks like one: the receive path sniffs magic and drops bytes that don't
    # match their declared MIME, so a purely random "image/png" would be
    # rejected as poison rather than exercising the transfer.
    p = tempfile.mkstemp(prefix="clippy-blob-")[1]
    data = magic + os.urandom(size - len(magic))
    open(p, "wb").write(data)
    return p, data, hashlib.sha256(data).hexdigest()

bp, bdata, bhash = _blob(6 * 1024 * 1024, magic=b"\x89PNG\r\n\x1a\n")
class IMG:
    kind = "image"; mime = "image/png"; image_path = bp; size = len(bdata)
    filename = None; hash = bhash; text = None
A._broadcast_entry(IMG())
for _ in range(80):
    time.sleep(0.2)
    if recv.get("copyimg"):
        break
assert recv.get("copyimg") == bhash, "image bytes mismatch on receive"
print("8. image streamed across + integrity verified")
assert prog and prog[-1][2] is True and prog[-1][0] == prog[-1][1], "progress incomplete"
print(f"9. progress fired ({len(prog)} updates) only for the big transfer")

fp, fdata, fhash = _blob(6 * 1024 * 1024)
recv.clear()
class FIL:
    kind = "file"; mime = "application/pdf"; image_path = fp; size = len(fdata)
    filename = "doc.pdf"; hash = fhash; text = "doc.pdf"
A._broadcast_entry(FIL())
for _ in range(80):
    time.sleep(0.2)
    if recv.get("copyfile"):
        break
assert recv.get("copyfile"), "file not received"
assert hashlib.sha256(open(recv["copyfile"], "rb").read()).hexdigest() == fhash, "file mismatch"
os.unlink(recv["copyfile"])
print("10. arbitrary file streamed across + integrity verified")

xp, xdata, xhash = _blob(3 * 1024 * 1024)
_caps["sync_max_bytes"] = 1 * 1024 * 1024
recv.clear()
class BIG:
    kind = "image"; mime = "image/png"; image_path = xp; size = len(xdata)
    filename = None; hash = xhash; text = None
A._broadcast_entry(BIG())
time.sleep(0.8)
assert not recv, "oversize transfer should be refused"
print("11. oversize transfer refused (size cap)")

# A peer's MIME label is a claim about bytes we never read ourselves. Measured
# case: macOS puts public.tiff on the pasteboard for most copied images and the
# mac backend labels it image/png, so uncompressed TIFF arrived wearing a PNG
# label — stored *and* pushed onto the clipboard, where every consumer that
# trusted the label pasted an empty image.
_vet = A._verified_image_mime
_PNG8 = b"\x89PNG\r\n\x1a\n"
assert _vet(_PNG8, "image/png", {}) == "image/png", "valid PNG should pass untouched"
assert _vet(b"MM\x00*rest", "image/png", {}) == "image/tiff", "TIFF-as-PNG should be relabelled"
assert _vet(b"\xff\xd8\xffx", "image/jpg", {}) == "image/jpg", "jpg/jpeg alias must not churn"
assert _vet(b"RIFF....WEBP", "image/webp", {}) == "image/webp", "unknown magic passes through"
assert _vet(b"\xd6\xa6\x07\x00" + _PNG8, "image/png", {}) is None, "length-prefixed PNG must be dropped"
assert _vet(b"not an image at all", "image/png", {}) is None, "unidentifiable bytes must be dropped"
print("12. mislabelled inbound media relabelled or rejected")

A.stop(); B.stop()
print("\n✅ ALL SYNC CORE TESTS PASSED")
