# Changelog

User-facing. Engineering detail lives in `docs/DEVLOG.md`. `CLAUDE.md` is the
current map of the pack; `docs/HANDOVER_*.md` and `BETA_NOTES.md` are
historical and should not be read as the state of this release.

## 2.0.0 — 2026-09-05

A full release, not a beta. The hop cache, the pin, the media slots and the
tone tools that accumulated on side branches since 1.1 are in this tree, plus
four new behaviours.

### New

- **`master_audio_file`** (MEDIA). One continuous take every hop lip-syncs
  to. Empty (the default) is off and does not change existing renders. When
  set: each hop is locked to a window of that file on the same clock as the
  picture; delivered audio is a passthrough of the take, no VAE round trip.
  The beat still needs the words in `<d>[English] ...</d>`.
- **`last_frame_guide`** (RUN, join & pin). `off` (default),
  `before_restart`, or `still`. It AddGuide-pins `start_image` at a hop's
  last pixel frame, so the hop *ends* on the photograph and a following
  restart — which opens on that same photograph — reads as a match cut
  rather than a jump. **`before_restart` is the setting to use:** it guides
  only a hop whose next shot is `anchor: "restart"`. `still` guides every
  hop, which also overrides an authored `framing` directive at every hop
  ending — a shot set `framing: close` plays close and then snaps to the
  still's wider framing in about 0.6 s, and the next hop pushes back in.
  Use `still` only when no shot authors a framing. Needs a start image.
  Neither becomes the next hop's frame 0.
- **`anchor: "restart"`** on a shot. That hop is a chain start: the start
  image is frame 0, nothing is relayed from the previous hop. It is a cut.
  Pair it with `join: hard_cut`. Restart hops now write their **full
  length** — they used to drop the 0.9 s overlap as if they were a
  continuation.
- **`refs` on a shot.** Which register stills ride that hop. Omit the field
  for the register default (unscheduled stills on chain starts, off
  continuations). `[]` is none. A filled list is those tags only, in that
  order. Unknown tags fail on the queue.

### Already on the 1.2 tree, now in the release

- Three reference-clip slots and three voice slots. Numbering is dense.
- Lab tone anchor (`tone_compensate=anchor`, `tone_anchor_ref` hop1 / still).
- `pin_mech` (auto / motion_context / addguide).
- Hop cache: safetensors latent sidecar (no pickle), master buffer spilled
  to a delete-on-close mapping above 2 GiB.
- **Seam report** takes the chain's `info` output as an input and reads the
  join frames the render actually wrote. Wire it: with a restart in the
  chain the hop lengths are no longer uniform, and the `hops` / `overlap`
  widgets cannot describe that — on a 4-hop chain restarting at hop 4 they
  put the seams six, eleven and sixteen frames out, far enough to measure
  the flat middle of a hop and report three visible seams as invisible.
- Per-hop cache keys for pin, overlap, tone and `pin_to_qwen`, so hop 1
  survives those A/Bs.

### One change that affects every render

The master frame buffer is **fp16** rather than fp32, which halves the
largest allocation in the pack — an 8x15 s chain at 1280x736 goes from about
29 GB to 14.4 GB, and half the disk I/O when it spills. The buffer is
delivery-only: nothing in the conditioning path reads it back, and tone
correction still happens in fp32 before the write.

It is not bit-identical, and the number is small but real. The encode
truncates, so a value fp16 nudges below an integer boundary loses one 255th.
Measured over two million samples: **2.06% of pixels move, every one of them
by exactly 1, none by more** — well under the h264 encode's own error. The
`images` output is therefore an fp16 IMAGE. Core's save and preview paths
take it; a third-party node that assumes fp32 has not been tested against it.

### Verified on a GPU

- The audio lock joins across hops on one take: hop 1 `[0.00s-8.00s]`, hop 2
  `[7.08s-15.08s]`, delivered as a passthrough, drift +0 ms. Empty really is
  off — the generated voice comes back.
- `anchor: "restart"` stays in the shot with a fresh seed, and writes its
  full length (a 4-hop chain restarting at hop 4 delivers 724 frames, not
  the 702 the old formula gave).
- `last_frame_guide` turns the restart jump into a match cut, and `still`
  fights an authored framing as described above.
- fp16 survives delivery.

### Still not verified

- Whether `refs: []` on a restart hop keeps the room instead of the identity
  portrait. The diagnosis behind that field came from frames, not from this
  code path.
- Anything longer than 4 hops at 8 s. The texture ratchet is unchanged and
  3-5 hops is still the honest limit.
- Three reference clips or three voices at once, on a card.

Saved 1.1 workflows load. New widgets were appended, not inserted, and no
existing default changed.

**A hop cache from 1.1 is fully invalidated by this release, deliberately.**
The model fingerprint now identifies the base checkpoint, which it did not
before — an int8 build and a bf16 build of the same architecture under the
same LoRA stack produced byte-identical keys, so the cache could serve frames
rendered under the other checkpoint. Closing that moves every key. The old
entries are never served, and the first sweep after the upgrade reclaims
them; their pickle-era latent sidecars are never read (that format is
retired) but are now counted against the budget and deleted with their entry
rather than orphaned. The practical effect is that the first chain after
upgrading re-renders in full.

## 1.1.1 — 2026-09-05

Core calls are keyword-only. Fixes `got multiple values for argument
'ref_image_size'` on hop 1 for ComfyUI builds that order MiniMax H3
parameters differently.

## 1.1.0 — 2026-09-03

Tabbed editor, `render_from` / `render_through` range, retention text on
every hop a still rides, computed canvas, per-hop reference keys.

## 1.0.x — 2026-09-02

First full release (WRITE panel, schema, rail). Patch releases cleared
registry-scanner findings and the WRITE-panel "no model is selected" bug.
