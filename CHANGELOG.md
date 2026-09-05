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
- **`last_frame_guide`** (RUN, join & pin). `off` (default) or `still`.
  `still` AddGuide-pins `start_image` at the last pixel frame of every hop,
  so a pin-less hop has a bound at both ends. It does **not** become the
  next hop's frame 0. Needs a start image. The end of every hop is pulled
  toward the still — a chroma gap can pulse.
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
- Per-hop cache keys for pin, overlap, tone and `pin_to_qwen`, so hop 1
  survives those A/Bs.

### Honest gaps

These were not rendered on a GPU before this tag. They are the first things
to check on a real card; see the test plan if you have one.

- Whether the sampler honours the audio-lock `noise_mask` (1 on video, 0 on
  audio). An ignored mask would still *sound* like the take (passthrough)
  while the mouth followed a generated voice.
- Whether a last-pixel AddGuide actually bounds a pin-less hop.
- Whether `refs: []` on a restart hop keeps the room instead of the
  identity portrait.

Saved 1.1 workflows load. New widgets were appended, not inserted:
`master_audio_file` then `last_frame_guide`. Older graphs get the empty /
`off` defaults.

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
