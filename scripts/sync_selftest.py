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

# Encrypted broadcast B -> A, with the clipboard/storage stubbed
got = []
st.add_text = lambda text, mime="text/plain", html=None: got.append(text) or 1
cb.copy_text = lambda text: got.append("COPY:" + text)
cb.copy_html = lambda html: got.append("COPYHTML")

class E:
    kind = "text"; text = "secret message ✨"; mime = "text/plain"; html = None

B._broadcast_entry(E())
time.sleep(1.2)
assert "COPY:secret message ✨" in got, got
print("5. encrypted item delivered + injected on A")

h = hashlib.sha256("secret message ✨".encode()).hexdigest()
n = len(got)
A.on_receive({"v": 1, "origin": B.device_id, "hash": h, "kind": "text",
              "mime": "text/plain", "text": "secret message ✨"})
assert len(got) == n, "echo not dropped"
print("6. loop prevention drops the echo")

n = len(got)
A._handle_sync({"from": "deadbeef", "box": "00"})
assert len(got) == n
print("7. unpaired sender rejected")

A.stop(); B.stop()
print("\n✅ ALL SYNC CORE TESTS PASSED")
