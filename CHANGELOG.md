# Changelog

User-facing. The files named here are in the repository, not in the installed
pack — the published package excludes them. Engineering detail lives in
`docs/DEVLOG.md`. `CLAUDE.md` is the current map of the pack;
`docs/HANDOVER_*.md` and `BETA_NOTES.md` are historical and should not be read
as the state of this release.

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
- **SWAP**, a fifth tab. A one-hop identity swap from a reference clip: the
  clip supplies the motion and the scene, a still from the REFERENCES rail
  supplies the person. **Write** drafts it, **Accept** writes exactly one
  shot plus the clip's description and never touches `ref_plan` -- your
  register is not rewritten. Contributed by @frankyi, then rebuilt from its
  own prose rather than merged.

  Four named modes, because at cfg 1.0 there is no negative branch and a
  mode that merely *omits* the swap line does not keep the clip's person --
  the identity photograph is in front of the encoder either way and governs
  the subject anyway. Each mode says positively what stays:
  `replace_person` (face, build, hairstyle and wardrobe),
  `head_swap` (face, hair and skin tone; the body, posture, hands and every
  garment stay with the clip), `face_only` (features only), and
  `keep_person` (swaps nobody -- the clip is a scene and motion plate, and
  the identity picker greys out). The taxonomy follows PromptMasterLD's edit
  laws; the prose is written fresh for H3 beats.

  Alongside them: **background** from the clip, from a `@tag` picture, or
  free; and an optional **wardrobe plate**, a `@tag` whose garment is *worn*
  -- draping on the body in frame and creasing where it bends -- rather than
  pasted.

  **Two things to get right, both of which cost renders to find out.** Do
  not run MEDIA's describe on the clip before a swap: that caption reaches
  the encoder as what `<Video 1>` *is*, and a caption naming a person asks
  for the person you are replacing. Four consecutive "head swap doesn't
  work" reports traced to that, a missing frame sequence and a weak
  citation -- no broken code among them. SWAP now warns when a caption names
  somebody. And drop the clip to about 0.3 MP: a reference clip's decode
  area is its token count, and its token count is its influence, so a
  full-size plate out-argues a single photograph.

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
- SWAP `head_swap` on a 0.3 MP clip with no clip caption: the head is
  replaced, the body and its motion stay with the clip, and no colour or
  hair from the plate bleeds through.

### Still not verified

- Whether `refs: []` on a restart hop keeps the room instead of the identity
  portrait. The diagnosis behind that field came from frames, not from this
  code path.
- Anything longer than 4 hops at 8 s. The texture ratchet is unchanged and
  3-5 hops is still the honest limit.
- Three reference clips or three voices at once, on a card.
- SWAP's wardrobe plate and picture background. Both were cited-but-not-
  scheduled until the fix in this release and could never have rendered;
  the fix is covered offline, not on a GPU. `face_only` and `keep_person`
  have not been run either.

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
