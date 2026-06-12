#!/usr/bin/env python3
"""Self-test: LAN sync survives a peer's device_id regenerating (no re-pair).

A peer's ``device-id`` file can regenerate while its keypair (``identity.key``)
stays the same — trust must follow the stable key, not the random device_id.
Mirrors ``scripts/sync_selftest.py``'s two-in-process-engines harness. Proves:
 - a drifted sender is still received (crypto-matched + entry migrated)
 - a stale duplicate entry is deduped by fingerprint

Run:  PYTHONPATH=. python3 scripts/sync_drift_test.py
"""
import hashlib
import sys
import tempfile
import time
import uuid

sys.path.insert(0, ".")
from clippy import sync          # noqa: E402
import clippy.storage as st      # noqa: E402
import clippy.clipboard as cb    # noqa: E402

if not sync.sync_available():
    print("FAIL: pynacl/zeroconf not installed")
    raise SystemExit(1)

A = sync.SyncEngine(port=48011, state_dir=tempfile.mkdtemp())
B = sync.SyncEngine(port=48012, state_dir=tempfile.mkdtemp())
A.start(); B.start()

# Pair (inject A's addr into B in case mDNS multicast is blocked).
B._peers_online.setdefault(A.device_id, ("127.0.0.1", 48011, "A"))
code = A.enter_pairing()
assert B.join_pairing(code).get("ok") and B.device_id in A.trusted
print("1. paired mutually")

got = []
st.add_text = lambda text, mime="text/plain", html=None: got.append(text) or 1
cb.copy_text = lambda text: got.append("COPY:" + text)


def send(txt):
    h = hashlib.sha256(txt.encode()).hexdigest()

    class E:
        kind = "text"; text = txt; mime = "text/plain"; html = None; hash = h
    B._broadcast_entry(E())


send("hello-1"); time.sleep(1.0)
assert "COPY:hello-1" in got, ("baseline failed", got)
print("2. baseline B->A delivery works")

# --- DRIFT: B's device-id regenerates; keypair (identity.key) unchanged ---
old_b = B.device_id
B.device_id = uuid.uuid4().hex
assert old_b in A.trusted and B.device_id not in A.trusted
print(f"3. B device_id drifted {old_b[:8]} -> {B.device_id[:8]} (same key)")

send("hello-2"); time.sleep(1.0)
assert "COPY:hello-2" in got, ("drifted sender was rejected", got)
print("4. drifted sender still received (matched by key, not device_id)")
assert B.device_id in A.trusted and old_b not in A.trusted, list(A.trusted)
print("5. A's trusted entry migrated to new id; old id removed")

# --- phantom dedupe (what re-pairing leaves behind) ---
A.trusted[old_b] = dict(A.trusted[B.device_id])          # inject a stale dup
fp = sync._fp_of(A.trusted[B.device_id]["pubkey"])
A._adopt_peer_id(fp, B.device_id)
assert B.device_id in A.trusted and old_b not in A.trusted, list(A.trusted)
print("6. stale duplicate deduped by fingerprint")

A.stop(); B.stop()
print("\n✅ DEVICE-ID DRIFT SELF-HEAL TESTS PASSED")
