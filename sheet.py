"""Contact sheet: the whole chain as one readable image.

An 8-hop chain is 114 seconds of video. Finding the hop that broke means
scrubbing, and scrubbing is how a defect that is obvious in a still gets missed
-- the 7.1-sigma cut at f1098 sat in a plan that passed every automated check
and in a video nobody watched frame by frame.

This renders one row per hop: the hop's first and last delivered frame side by
side, with its beat, its directives and what actually happened to it (seed,
steps, cache hit, tone correction). One glance says which hop broke and what it
was told to do.

The same builder serves `dry_run`, where there are no frames yet: rows arrive
with `first`/`last` as None and the row becomes a text panel, so a plan can be
read end to end before a single sampler step.

Nothing here is allowed to lose a render. Pillow is a hard ComfyUI dependency
and a system font is nearly always present, but a sheet that raised would throw
away a finished chain -- so every failure path returns the 1x1 placeholder and
prints a note instead.
"""
from __future__ import annotations

import os

import torch

TAG = "HTCSheet"

# Fixed sheet width. Wide enough for two 16:9 thumbnails plus a readable text
# column, and a round number in the output video's own scale.
SHEET_W = 1280
THUMB_H = 168
PAD = 14
ROW_GAP = 2

BG = (14, 14, 14)
ROW_BG = (24, 24, 24)
ROW_BG_ALT = (30, 30, 30)
FG = (232, 232, 232)
DIM = (150, 150, 150)
ACCENT = (232, 255, 71)   # --h3-accent
WARN = (255, 138, 92)

_FONT_CANDIDATES = (
    # Windows
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\tahoma.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
)

_font_cache = {}


def _font(size, bold=False):
    """A truetype face at `size`, falling back all the way to Pillow's default."""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    from PIL import ImageFont

    cands = list(_FONT_CANDIDATES)
    if bold:
        cands = [c.replace("segoeui.ttf", "segoeuib.ttf")
                  .replace("arial.ttf", "arialbd.ttf")
                  .replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
                  .replace("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf")
                 for c in cands] + cands
    f = None
    for path in cands:
        try:
            if os.path.exists(path):
                f = ImageFont.truetype(path, size)
                break
        except Exception:
            continue
    if f is None:
        # matplotlib ships DejaVu and is present in most ComfyUI installs.
        try:
            import matplotlib
            p = os.path.join(os.path.dirname(matplotlib.__file__),
                             "mpl-data", "fonts", "ttf",
                             "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
            if os.path.exists(p):
                f = ImageFont.truetype(p, size)
        except Exception:
            pass
    if f is None:
        try:
            f = ImageFont.load_default(size=size)   # Pillow >= 10.1
        except Exception:
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def placeholder():
    """The 1x1 black IMAGE returned when no sheet was built."""
    return torch.zeros((1, 1, 1, 3), dtype=torch.float32)


def _wrap(draw, text, font, max_w):
    """Greedy word wrap to `max_w` pixels. -> list of lines."""
    words = str(text or "").split()
    if not words:
        return []
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _to_pil(frame):
    """A [H,W,3] float 0..1 tensor -> PIL RGB image."""
    from PIL import Image
    import numpy as np
    a = frame.detach().float().clamp(0, 1).cpu().numpy()
    return Image.fromarray((a * 255.0 + 0.5).astype(np.uint8), mode="RGB")


def _thumb(frame, box_w, box_h):
    from PIL import Image
    im = _to_pil(frame)
    im.thumbnail((box_w, box_h), Image.LANCZOS)
    return im


def build(rows, title="", width=SHEET_W):
    """Render the sheet. -> IMAGE tensor [1,H,W,3] float 0..1.

    `rows` is a list of dicts:
        hop        1-based index (int)
        first,last [H,W,3] float tensors, or None for a text-only row
        beat       the authored beat
        directives dict of axis -> value
        meta       list of short strings shown dim under the beat
        note       optional string shown in the warning colour
    """
    try:
        return _build(rows, title, width)
    except Exception as e:      # never lose a render over a picture
        print("[%s] contact sheet skipped (%s: %s)" % (TAG, type(e).__name__, e),
              flush=True)
        return placeholder()


def _build(rows, title, width):
    from PIL import Image, ImageDraw

    if not rows:
        return placeholder()

    f_title = _font(21, bold=True)
    f_hop = _font(19, bold=True)
    f_body = _font(15)
    f_small = _font(13)

    # Thumbnails keep the source aspect; two of them share the left column.
    have_frames = any(r.get("first") is not None for r in rows)
    if have_frames:
        src = next(r["first"] for r in rows if r.get("first") is not None)
        ar = float(src.shape[1]) / float(src.shape[0])   # W/H
        tw = int(round(THUMB_H * ar))
        # Two thumbs must not eat more than 60% of the sheet.
        max_tw = int((width * 0.60 - PAD * 3) / 2)
        if tw > max_tw:
            tw = max_tw
        thumbs_w = tw * 2 + PAD
    else:
        tw = 0
        thumbs_w = 0

    text_x = PAD + (thumbs_w + PAD if thumbs_w else 0)
    text_w = width - text_x - PAD

    # --- measure ---------------------------------------------------------
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    laid = []
    for r in rows:
        beat_lines = _wrap(probe, r.get("beat") or "(continues)", f_body, text_w)
        note_lines = _wrap(probe, r.get("note") or "", f_small, text_w) if r.get("note") else []
        meta = " / ".join(str(m) for m in (r.get("meta") or []) if m)
        meta_lines = _wrap(probe, meta, f_small, text_w) if meta else []
        h_text = (26                                   # hop line
                  + 19 * max(1, len(beat_lines))
                  + (6 + 17 * len(note_lines) if note_lines else 0)
                  + (6 + 17 * len(meta_lines) if meta_lines else 0))
        h = max(THUMB_H if thumbs_w else 0, h_text) + PAD * 2
        laid.append((r, beat_lines, note_lines, meta_lines, h))

    head_h = (PAD * 2 + 26) if title else 0
    total_h = head_h + sum(h for _, _, _, _, h in laid) + ROW_GAP * max(0, len(laid) - 1) + PAD

    img = Image.new("RGB", (int(width), int(total_h)), BG)
    d = ImageDraw.Draw(img)

    y = 0
    if title:
        d.text((PAD, PAD), title, font=f_title, fill=ACCENT)
        y = head_h

    for idx, (r, beat_lines, note_lines, meta_lines, h) in enumerate(laid):
        d.rectangle([0, y, width, y + h - 1],
                    fill=ROW_BG if idx % 2 == 0 else ROW_BG_ALT)
        if thumbs_w:
            for k, key in enumerate(("first", "last")):
                fr = r.get(key)
                bx = PAD + k * (tw + PAD)
                if fr is None:
                    d.rectangle([bx, y + PAD, bx + tw, y + PAD + THUMB_H],
                                outline=(60, 60, 60))
                    continue
                th = _thumb(fr, tw, THUMB_H)
                img.paste(th, (bx, y + PAD))
                d.text((bx + 4, y + PAD + THUMB_H - 17), key,
                       font=f_small, fill=(210, 210, 210),
                       stroke_width=2, stroke_fill=(0, 0, 0))

        ty = y + PAD
        dirs = r.get("directives") or {}
        dtxt = "  ".join("%s=%s" % (k, v) for k, v in dirs.items()) or "no directives"
        label = "HOP %s" % r.get("hop")
        d.text((text_x, ty), label, font=f_hop, fill=ACCENT)
        hw = d.textlength(label, font=f_hop)
        d.text((text_x + hw + 10, ty + 3), dtxt, font=f_small, fill=DIM)
        ty += 26
        for ln in (beat_lines or ["(continues)"]):
            d.text((text_x, ty), ln, font=f_body, fill=FG)
            ty += 19
        if note_lines:
            ty += 6
            for ln in note_lines:
                d.text((text_x, ty), ln, font=f_small, fill=WARN)
                ty += 17
        if meta_lines:
            ty += 6
            for ln in meta_lines:
                d.text((text_x, ty), ln, font=f_small, fill=DIM)
                ty += 17

        y += h + ROW_GAP

    import numpy as np
    a = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(a).unsqueeze(0)


def small(frame, h=THUMB_H):
    """Downscale one [H,W,3] frame to `h` pixels tall, for storing in a row.

    The sheet only ever shows thumbnails, and a full 1280x736 float frame is
    11 MB -- two per hop across eight hops is 180 MB held for the length of the
    render for no reason. Shrinking at collection time keeps it under a MB.
    """
    if frame is None:
        return None
    try:
        import torch.nn.functional as F
        f = frame.detach().float().unsqueeze(0).permute(0, 3, 1, 2)
        H, W = int(f.shape[2]), int(f.shape[3])
        if H <= h:
            return frame.detach().float().clone()
        w = max(1, int(round(W * (float(h) / float(H)))))
        out = F.interpolate(f, size=(int(h), w), mode="area")
        return out.permute(0, 2, 3, 1)[0].contiguous()
    except Exception:
        return frame.detach().float().clone()
