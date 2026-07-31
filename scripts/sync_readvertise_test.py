#!/usr/bin/env python3
"""Self-test: the periodic mDNS refresh republishes a *changed* local address.

`readvertise()` runs on a timer so peers rediscover us after a blip. It used to
re-announce `self._info` verbatim — a snapshot taken at startup — so after a
network change it kept broadcasting the old address indefinitely.

Measured: a laptop paired on the LAN joined an iPhone hotspot, registered
172.20.10.8, came back to the LAN, and went on advertising the hotspot address.
The peer relearned it (by design — peers trust the advertisement) and every send
then timed out against an unroutable host until the app was restarted:

    text: send to <peer> FAILED after 3x over [('172.20.10.8', 47823)]: TimeoutError

Only one candidate in that list, because the freshly advertised address had also
overwritten the stored fallback.

Run:  PYTHONPATH=. python3 scripts/sync_readvertise_test.py
"""
import pathlib
import socket
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clippy import sync  # noqa: E402

if not sync.sync_available():
    print("FAIL: pynacl/zeroconf not installed")
    raise SystemExit(1)

# Never touch the real <data>/sync.log — this is a test, not traffic.
sync._SYNC_LOG = pathlib.Path(tempfile.mkdtemp()) / "sync.log"

LAN = "192.168.1.17"
HOTSPOT = "172.20.10.8"

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")


class FakeZC:
    """Records what would go on the wire."""

    def __init__(self):
        self.updated, self.registered, self.unregistered = [], [], []

    def update_service(self, info):
        self.updated.append(info)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)


def engine_at(ip):
    eng = sync.SyncEngine(port=48123, state_dir=tempfile.mkdtemp())
    eng._zc = FakeZC()
    eng._info = eng._service_info(ip)
    return eng


def published(eng):
    """The address most recently handed to zeroconf."""
    sent = eng._zc.updated or eng._zc.registered
    if not sent:
        return None
    addrs = sent[-1].addresses or []
    return socket.inet_ntoa(addrs[0]) if addrs else None


def main():
    real_local_ip = sync._local_ip
    try:
        # 1. Address changed under us: the refresh must publish the new one.
        eng = engine_at(HOTSPOT)
        sync._local_ip = lambda: LAN
        eng.readvertise()
        check("a changed address must be republished", published(eng) == LAN,
              f"still advertising {published(eng)} — this is the bug")
        check("the record must carry the new address",
              socket.inet_aton(LAN) in (eng._info.addresses or []))

        # TXT properties must survive the rebuild, or peers lose the id/fp that
        # identifies us and _adopt_peer_id can't heal device-id drift.
        props = {k.decode(): v for k, v in (eng._info.properties or {}).items()}
        check("rebuilt record keeps its TXT properties",
              {"id", "name", "fp"} <= set(props), str(sorted(props)))
        check("rebuilt record keeps our device id",
              props.get("id", b"").decode() == eng.device_id)

        # 2. Nothing changed: still refresh (that's the point of the timer), but
        #    don't churn the record.
        eng = engine_at(LAN)
        before = eng._info
        sync._local_ip = lambda: LAN
        eng.readvertise()
        check("an unchanged address must not rebuild the record",
              eng._info is before)
        check("an unchanged address must still re-announce",
              len(eng._zc.updated) == 1)

        # 3. No route: _local_ip falls back to loopback. Advertising 127.0.0.1
        #    would be worse than keeping a stale address.
        eng = engine_at(LAN)
        sync._local_ip = lambda: "127.0.0.1"
        eng.readvertise()
        check("loopback must never be advertised", published(eng) == LAN,
              f"advertised {published(eng)}")

        # 4. update_service failing must still get the new address out.
        eng = engine_at(HOTSPOT)

        def boom(_info):
            raise RuntimeError("update_service unsupported")

        eng._zc.update_service = boom
        sync._local_ip = lambda: LAN
        eng.readvertise()
        check("fallback path must register the new record",
              eng._zc.registered and
              socket.inet_aton(LAN) in (eng._zc.registered[-1].addresses or []))
        check("fallback path must unregister the OLD record",
              eng._zc.unregistered and
              socket.inet_aton(HOTSPOT) in (eng._zc.unregistered[-1].addresses or []))

        # 5. Never blow up when discovery isn't running.
        eng = sync.SyncEngine(port=48124, state_dir=tempfile.mkdtemp())
        try:
            eng.readvertise()          # _zc and _info are both None
        except Exception as exc:
            check("readvertise with no discovery must be a no-op", False, repr(exc))
    finally:
        sync._local_ip = real_local_ip

    if failures:
        print("FAIL: mDNS refresh does not track the local address:")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: address changes are republished, loopback and no-ops are not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
