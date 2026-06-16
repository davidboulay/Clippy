# Brief: ship the `_seen` sync fix to macOS (cut v1.4.2)

**Audience:** the macOS Claude Code session working in the `Clippy` repo.
**Author:** the Linux (COSMIC) session.
**Date:** 2026-06-16.

## TL;DR

A shared-core sync bug was found and fixed on `main` (**PR #20**, merged). It's
in `sync.py`, so **both platforms are affected and both are fixed by the same
commit** — but the macOS app users run is the **v1.4.1 release, which predates
the fix**, so Macs still misbehave until a new build ships. **Please verify on
macOS and cut `v1.4.2`** (the release workflow builds the `.dmg` from the tag).
The Linux daemon here is already running the fixed code.

## The bug (user-visible)

Copying a file/text on Linux often did **not** appear on the paired Mac —
reproducibly for anything already exchanged once, while *brand-new* clips synced
fine. It looked file-type-correlated (PDFs synced, txt/csv didn't), but that was
coincidental: the txt/csv had been copied before; the PDFs were fresh. Same class
of symptom in either direction.

## Root cause

`sync.py`'s echo-suppression cache `_seen` had **no expiry**. `_seen_add` stored a
timestamp but `_seen_has` only checked membership, so a content-hash stayed
"seen" until 256 newer hashes evicted it or the daemon restarted. All three sync
gates consult it: broadcast (`_broadcast_entry`), send-dedup, and receive
(`_handle_sync` / `_handle_media`). So once a clip's hash was known to a daemon —
because it was sent, **or echoed back by the peer on connect** — re-copying it was
silently dropped on the sender, *and* a re-delivery was rejected on the receiver.

Instrumented trace of a stuck file on a freshly-started daemon:

```
broadcast_entry id=701 kind=file name='Back In Stock…csv' hash=a73bf75324 seen=True
 -> SKIP (no hash or already seen)
```

After the fix, the same copy:

```
broadcast_entry id=701 … seen=False
media: 'Back In Stock…csv' delivered to <Mac> OK
```

## The fix (already on `main`, PR #20)

Give `_seen` a TTL and have `_seen_has` honor the already-stored timestamp:

```python
_SEEN_TTL = 30   # seconds
def _seen_has(self, h):
    ts = self._seen.get(h)
    return ts is not None and (time.time() - ts) < _SEEN_TTL
```

30 s comfortably covers the inject→re-capture echo round-trip (a couple of
seconds) while letting a deliberate re-copy re-broadcast afterward. No new state.
`scripts/sync_delivery_test.py` gained **case 5** (a `_seen` entry past the TTL is
no longer "seen"); selftest + drift + delivery are all green in CI.

## What we need from the macOS session

1. **Pull `main`** (has PR #20). Confirm `clippy/sync.py` has `_SEEN_TTL`.
2. **Sanity-check macOS sync** with the fix:
   - Fresh copy Mac→Linux and Linux→Mac still works.
   - **Re-copy** the *same* item after ~a minute → it syncs again (the bug).
   - No echo storm / duplicate loop (copy once, confirm it lands once — the 30 s
     window still suppresses the immediate bounce). The mac changeCount watcher
     already ignores our own writes, so this should be clean.
3. **Bump `clippy/__init__.py` `__version__` to `1.4.2`** via a PR (protected
   `main`: branch → PR → required `test` check → self-merge).
4. **Tag and push `v1.4.2`** — the release workflow builds the Linux `.deb` **and**
   the macOS `.dmg` and publishes the GitHub release. The user then reinstalls the
   `.dmg` on the Mac and the fix is live there.

## What `v1.4.2` will include (everything merged since `v1.4.1`)

- **#20** fix(sync): expire the `_seen` cache so re-copied clips sync again ← the reason for this release
- **#18** fix(mac): received-clip sound (`_on_received` callback)
- **#17** feat(linux): live-refresh the open panel when clips arrive
- **#16** fix(mac): live-refresh the open panel when clips arrive
- **#15** fix(sync): retry + try-both-addresses delivery (intermittent drops)
- **#13/#14/#19** docs/screenshot housekeeping

## Notes / process

- `main` is protected: PRs + the required `test` CI check, no direct pushes.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Don't redesign anything — the fix is intentionally minimal. Just verify on
  macOS, bump, tag.
- Cosmetic, separate, **not** blocking this release: a recovered/synced *file*
  pastes with its content-hash blob name (e.g. `a8525b….txt`) rather than the
  original filename, on **both** platforms (blobs are stored as `<sha256><ext>`).
  Worth a future polish PR (carry `entry.filename` through paste), but unrelated
  to the sync bug.
