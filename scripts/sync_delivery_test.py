#!/usr/bin/env python3
"""Self-test for clippy LAN sync *delivery hardening*.

Proves the retry / try-both-addresses behaviour added to fight intermittent,
silently-dropped clips:
  1. delivery falls back to the stored address when the (first) mDNS address is
     unreachable — a stale mDNS record or dual-homed peer no longer loses the clip;
  2. the hash is marked 'seen' only after a send *succeeds*, so a totally failed
     send doesn't suppress a later re-copy of the same content.

Run:  PYTHONPATH=. python3 scripts/sync_delivery_test.py
Needs: python3-nacl + python3-zeroconf
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
    print("FAIL: pynacl/zeroconf not installed")
    raise SystemExit(1)

A = sync.SyncEngine(port=48051, state_dir=tempfile.mkdtemp())
B = sync.SyncEngine(port=48052, state_dir=tempfile.mkdtemp())
A.start(); B.start()

# Pair them (inject addr in case mDNS is blocked on the CI runner).
B._peers_online.setdefault(A.device_id, ("127.0.0.1", 48051, "A"))
code = A.enter_pairing()
assert B.join_pairing(code).get("ok"), "pairing failed"
print("1. paired A<->B")

got = []
st.add_text = lambda text, mime="text/plain", html=None: got.append(text) or 1
cb.copy_text = lambda text: got.append("COPY:" + text)
cb.copy_html = lambda html: got.append("COPYHTML")

# A fires its received-clip hook (macOS wires this to the copy sound, since its
# changeCount watcher is suppressed for our own writes).
received = []
A._on_received = lambda: received.append(1)

# --- 2. candidate-address ordering (mDNS first, then stored addr, de-duped) --
B._peers_online[A.device_id] = ("127.0.0.1", 48051, "A")
B.trusted[A.device_id]["addr"] = "10.0.0.9"
assert B._peer_addrs(A.device_id, B.trusted[A.device_id]) == \
    [("127.0.0.1", 48051), ("10.0.0.9", sync.config.SYNC_PORT)], "candidate order/dedup wrong"
print("2. _peer_addrs yields live mDNS addr first, stored addr as fallback")

# --- 3. delivery retries across addresses: first (bad) fails, second lands ---
peer = B.trusted[A.device_id]
_TXT = "fallback works ✨"
import json
env = {"v": 1, "origin": B.device_id, "ts": 0, "hash":
       hashlib.sha256(_TXT.encode()).hexdigest(), "kind": "text",
       "mime": "text/plain", "text": _TXT}
payload = json.dumps(env).encode()
B._deliver_text([("127.0.0.1", 9), ("127.0.0.1", 48051)], peer, env["hash"], payload)
time.sleep(0.5)
assert "COPY:" + _TXT in got, f"fallback delivery failed: {got}"
assert B._seen_has(env["hash"]), "hash should be 'seen' after a successful send"
assert received, "the _on_received hook should fire for a received clip"
print("3. delivered via the 2nd address after the 1st refused; seen + hook fired")

# --- 4. total failure leaves the hash un-seen (a later re-copy can retry) ----
_T2 = "this never arrives"
h2 = hashlib.sha256(_T2.encode()).hexdigest()
env2 = dict(env, text=_T2, hash=h2)
B._deliver_text([("127.0.0.1", 9)], peer, h2, json.dumps(env2).encode())  # only-bad addr
assert not B._seen_has(h2), "a fully-failed send must NOT mark the hash seen"
print("4. fully-failed send left the hash un-seen (a later re-copy will retry)")

# --- 5. 'seen' expires after the TTL so a deliberate re-copy re-syncs ---------
hx = hashlib.sha256(b"re-copy me later").hexdigest()
B._seen_add(hx)
assert B._seen_has(hx), "freshly-added hash should be seen (echo suppression)"
B._seen[hx] = time.time() - sync._SEEN_TTL - 1          # backdate past the TTL
assert not B._seen_has(hx), "an expired hash must no longer be 'seen' (re-copy re-syncs)"
print("5. 'seen' entry expires after the TTL — re-copying an item later re-syncs")

A.stop(); B.stop()
print("\n✅ DELIVERY HARDENING TESTS PASSED")
