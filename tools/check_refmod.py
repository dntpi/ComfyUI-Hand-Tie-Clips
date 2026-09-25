"""Offline checks for RefMod rows, strength, and the safetensors header.

    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\check_refmod.py

No VAE and no sampling. A file written here is the version-4 header Luisa's
loader reads, checked by key and shape.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import torch

_PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(_PACK)))
sys.path.insert(0, _PACK)

import refs as R          # noqa: E402
import refmod as M        # noqa: E402


def ck(name, ok, detail=""):
    print(("ok  " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(1)


def test_schedule():
    plan = R.parse_ref_plan(json.dumps({"refs": [
        {"tag": "cook", "source": "refmod", "file": "hero", "subject": 1,
         "shots": [1], "strength": 1},
        {"tag": "kitchen", "file": "kitchen.png", "retention": "reference"},
    ], "subjects": {"1": {"name": "the cook", "locked": "the same face"}}}))
    refs = plan["refs"]
    ck("still defaults to source image", refs[1]["source"] == "image")
    ck("refmod keeps a subfolder-free stem", refs[0]["file"] == "hero")
    wired = {1, 2}
    hop1 = [r["tag"] for r in R.active_refs(refs, 0, wired)]
    hop2 = [r["tag"] for r in R.active_refs(refs, 1, wired)]
    ck("hop 1 carries the refmod and the still", hop1 == ["cook", "kitchen"], str(hop1))
    ck("hop 2 does not carry a refmod pinned to hop 1", hop2 == ["kitchen"], str(hop2))
    none = R.select_for_shot(refs, [], wired)
    only = [r["tag"] for r in R.select_for_shot(refs, ["cook"], wired)]
    ck("an empty shot.refs list carries nothing", none == [])
    ck("shot.refs can name the refmod alone", only == ["cook"], str(only))
    text = R.resolve_tags(
        "@cook stands in @kitchen", {"kitchen": 2}, R.subjects(refs),
        subject_names={1: "the cook"})
    ck("a subject refmod resolves as the person, the still as a picture",
       text.startswith("The cook") and "<Picture 2>" in text, text)


def test_strength_and_file():
    latent = torch.arange(96, dtype=torch.float32).reshape(1, 24, 1, 2, 2)
    same = M.apply_strength(latent, 1)
    ck("strength 1 returns the stored latent", same is latent)
    weak = M.apply_strength(latent, 0.4)
    manual = 0.4 * latent + 0.6 * M._blur_latent(latent)
    ck("strength 0.4 is the blur mix", torch.allclose(weak, manual))
    ck("strength 0 drops the block", M.apply_strength(latent, 0) is None)
    block = M.visual_block({"kind": "image", "latent": latent}, 1)
    ck("image block carries the latent grid",
       block["kind"] == "image" and block["latent_h"] == 2 and block["latent_w"] == 2)

    meta = M.metadata_for("hero", "image", latent, "encode", "image", "1x2x2", "encode", 0)
    with tempfile.TemporaryDirectory() as tmp:
        stem = M.save_latent(tmp, "people/hero", latent, meta)
        path = os.path.join(tmp, "people", "hero.safetensors")
        ck("writer stem keeps the subfolder", stem == "people/hero", stem)
        got = M.read_meta(path)
        ck("header is version 4 refmod_meta",
           got and got.get("_format_version") == 4 and got.get("kind") == "image"
           and got.get("latent_h") == 2)
        from safetensors import safe_open
        with safe_open(path, framework="pt") as handle:
            keys = list(handle.keys())
        ck("standalone tensor key is latent", keys == ["latent"], str(keys))

        # A bundle is read as its first visual member.
        from safetensors.torch import save_file
        member = dict(meta)
        member["name"] = "hero_visual"
        bundle_path = os.path.join(tmp, "pack.safetensors")
        save_file({"ref_0": latent}, bundle_path, metadata={
            M.META_KEY: json.dumps({
                "_format_version": 5, "kind": "bundle", "name": "pack",
                "members": [member],
            })})
        M.roots = lambda: [tmp]
        try:
            loaded = M.load_visual("people/hero")
            ck("round trip keeps the grid",
               loaded["kind"] == "image" and tuple(loaded["latent"].shape) == (1, 24, 1, 2, 2))
            loaded_b = M.load_visual("pack")
            ck("a bundle yields its visual member",
               loaded_b["kind"] == "image" and loaded_b["tokens"] == 1)
        finally:
            # roots() is looked up on the module; leave the real one in place
            # for anything imported after this process. This script exits.
            pass

    cond = [[torch.zeros(1), {"minimax_refs": [
        {"kind": "image", "latent_h": 1, "latent_w": 1, "latent": torch.zeros(1, 24, 1, 1, 1)},
        {"kind": "image", "latent_h": 1, "latent_w": 1, "latent": torch.ones(1, 24, 1, 1, 1)},
    ]}]]
    pixels = torch.zeros(1, 8, 8, 3)
    other = torch.ones(1, 8, 8, 3)
    spliced = M.splice_blocks(
        cond, {"ref_image_1": other, "ref_image_2": pixels}, None,
        {id(pixels): block}, {})
    got_lat = spliced[0][1]["minimax_refs"][1]["latent"]
    ck("splice replaces only the refmod's picture slot", torch.allclose(got_lat, latent))
    ck("the other picture's block stays", spliced[0][1]["minimax_refs"][0]["latent_h"] == 1)
    empty = [[torch.zeros(1), {}]]
    skipped = M.splice_blocks(empty, None, None, {id(pixels): block}, {})
    ck("a hop that does not cite the mod is left alone", skipped is empty)


def test_kind_cap():
    refs = [
        {"tag": "a", "source": "image", "kind": "image", "shots": None, "slot": 1},
        {"tag": "b", "source": "refmod", "kind": "video", "shots": [2], "slot": 2},
    ]
    R.check_kind_caps(refs, media_videos=2)
    ck("one video mod beside two clips is inside the cap", True)
    try:
        R.check_kind_caps(refs, media_videos=3)
    except ValueError as exc:
        ck("a third clip plus a video mod is refused", "3 videos" in str(exc) or "at most" in str(exc), str(exc))
    else:
        ck("a third clip plus a video mod is refused", False, "no error")


def test_mod_plan():
    stills, clip, _v = M.parse_mod_plan("")
    ck("an empty mod_plan leaves the sockets in charge", stills == [] and clip is None)

    stills, clip, _v = M.parse_mod_plan(json.dumps(
        {"stills": [{"file": "face.png"}, "body.png"],
         "clip": {"file": "walk.mp4", "start": 1.5, "end": 3.0, "frames": 22}}))
    ck("stills keep their order and accept both spellings",
       stills == ["face.png", "body.png"], str(stills))
    ck("the clip window parses",
       clip == {"file": "walk.mp4", "start": 1.5, "end": 3.0, "frames": 22}, str(clip))

    stills, clip, _v = M.parse_mod_plan(json.dumps(["a.png", "b.png"]))
    ck("a bare list is a list of stills", stills == ["a.png", "b.png"] and clip is None)

    for bad, why in (
        (json.dumps({"stills": ["a.png", "a.png"]}), "twice"),
        (json.dumps({"stills": ["../a.png"]}), "plain filename"),
        (json.dumps({"clip": {"file": "c.mp4", "start": 3.0, "end": 1.0}}), "after"),
        ("{not json", "valid JSON"),
    ):
        try:
            M.parse_mod_plan(bad)
        except ValueError as exc:
            ck(f"mod_plan refuses {why}", why in str(exc), str(exc))
        else:
            ck(f"mod_plan refuses {why}", False, "no error")


def test_stack():
    a = torch.zeros(1, 24, 1, 8, 6)
    b = torch.ones(1, 24, 1, 8, 6)
    c = torch.ones(1, 24, 5, 8, 6) * 2
    ck("one source stacks to itself", M.stack_latents([a], "t") is a)
    stacked = M.stack_latents([a, b, c], "t")
    ck("two stills and a clip become one 7-frame latent",
       tuple(stacked.shape) == (1, 24, 7, 8, 6), str(tuple(stacked.shape)))
    ck("stacking keeps each source's own frames",
       bool((stacked[:, :, 0] == 0).all() and (stacked[:, :, 1] == 1).all()
            and (stacked[:, :, 2] == 2).all()))
    ck("a 7-frame stack reads back as a video mod",
       M._as_visual({"kind": "video"}, stacked, "t")["latent_t"] == 7)
    try:
        M.stack_latents([a, torch.zeros(1, 24, 1, 8, 8)], "t")
    except ValueError as exc:
        ck("a mismatched canvas is refused", "share a canvas" in str(exc), str(exc))
    else:
        ck("a mismatched canvas is refused", False, "no error")


def test_canvas():
    frames = torch.zeros(1, 1800, 1440, 3)
    tw, th = M.canvas_size(frames)
    ck("a 1440x1800 still lands on the 1024 short edge",
       (tw, th) == (1024, 1280), f"{tw}x{th}")
    ck("to_canvas honours that exact size",
       tuple(M.to_canvas(frames, tw, th).shape) == (1, 1280, 1024, 3))
    ck("fit_canvas still agrees with the pair",
       tuple(M.fit_canvas(frames).shape) == (1, 1280, 1024, 3))
    small = torch.zeros(1, 480, 640, 3)
    ck("a small still is never upscaled", M.canvas_size(small) == (640, 480))




def test_mod_voice():
    """The builder is where a character is assembled, so the voice is",
    named there and a citing row inherits it."""
    stills, clip, voice = M.parse_mod_plan(json.dumps(
        {"stills": ["a.png"], "voice": "hero_vo.wav"}))
    ck("a mod_plan carries a voice", voice == "hero_vo.wav" and stills == ["a.png"])
    ck("an absent voice is an empty string",
       M.parse_mod_plan(json.dumps({"stills": ["a.png"]}))[2] == "")
    ck("an empty widget still unpacks", M.parse_mod_plan("") == ([], None, ""))
    for bad, why in (
            ({"stills": ["a.png"], "voice": "a.png"}, "a picture as a voice"),
            ({"stills": ["a.png"], "voice": "sub/v.wav"}, "a path as a voice"),
            ({"clip": "v.mp3"}, "a sound file in the clip slot")):
        try:
            M.parse_mod_plan(json.dumps(bad))
        except ValueError:
            pass
        else:
            ck(why + " is refused", False)
    ck("a sound file in the clip slot is refused before the VAE", True)

def test_source_problem():
    """A failed source says why. "not in h3_refs" was wrong for two of
    the three reasons, and sent someone hunting for a file that was there."""
    import media as MED
    real = MED.resolve
    MED.resolve = lambda n, kinds=None: ("X" if str(n).startswith("here") else None)
    try:
        ck("an absent name still says not in h3_refs",
           M.source_problem("gone.png") == "gone.png is not in h3_refs")
        msg = M.source_problem("here.mp3")
        ck("a sound file is named as one, and says where a voice goes",
           "sound file" in msg and "voice slot" in msg, msg)
        # The bug this guards: a sentence for a file with nothing wrong
        # with it failed a build on its own good voice.
        ck("a present file of the right kind is no problem at all",
           M.source_problem("here.png") is None)
        ck("a present sound file is no problem in an audio slot",
           M.source_problem("here.mp3", ("audio",)) is None)
        ck("a picture in the clip slot is not called missing",
           "not in h3_refs" not in M.source_problem("here.png", ("video",)))
        ck("a blank name is not a problem", M.source_problem("") is None)
    finally:
        MED.resolve = real

def test_row_voice():
    """A voice on a row: parsed, capped, round-tripped, and described."""
    plan = R.parse_ref_plan(json.dumps({"refs": [
        {"tag": "hero", "source": "refmod", "file": "hero_clip",
         "voice": "hero_vo.wav", "shots": [1, 2]},
        {"tag": "kitchen", "file": "kitchen.png"},
    ]}))
    refs = plan["refs"]
    ck("a row voice survives parsing", refs[0]["voice"] == "hero_vo.wav")
    ck("a row without one reads empty", refs[1]["voice"] == "")

    # A path is refused the same way a still is: media.resolve would never
    # find it, so accepting it would only ever fail at load time.
    bad = R.parse_ref_plan(json.dumps({"refs": [
        {"tag": "hero", "file": "a.png", "voice": "sub/dir/vo.wav"}]}))
    ck("a directory component is stripped from a voice",
       bad["refs"][0]["voice"] == "vo.wav", bad["refs"][0]["voice"])

    try:
        R.parse_ref_plan(json.dumps({"refs": [
            {"tag": "hero", "file": "a.png", "vioce": "x.wav"}]}))
    except ValueError as exc:
        ck("a misspelled field still fails loudly", "unknown field" in str(exc))
    else:
        ck("a misspelled field still fails loudly", False, "no error")

    # Every row voice spends a standalone audio slot, a clip's included.
    stills = [dict(r, voice="v.wav", kind="image", shots=None)
              for r in R.parse_ref_plan(json.dumps({"refs": [
                  {"tag": f"s{i}", "file": f"{i}.png"} for i in range(3)]}))["refs"]]
    R.check_audio_caps(stills, media_voices=0)
    ck("three row voices are allowed", True)
    try:
        R.check_audio_caps(stills, media_voices=1)
    except ValueError as exc:
        ck("a fourth standalone voice is refused",
           "at most" in str(exc), str(exc))
    else:
        ck("a fourth standalone voice is refused", False, "no error")
    vids = [dict(r, kind="video") for r in stills]
    try:
        R.check_audio_caps(vids, media_voices=1)
    except ValueError:
        ck("a voice on a video row costs an audio slot", True)
    else:
        ck("a voice on a video row costs an audio slot", False, "no error")

    # describe() runs after run() has set `kind` from the loaded file --
    # which block a voice becomes is not knowable from the plan alone.
    ck("a voice reads as beside the picture before the kind is known",
       "(beside)" in R.describe(plan))
    refs[0]["kind"] = "video"
    line = R.describe(plan)
    ck("a clip's voice also reads as beside -- it is never fused",
       "(beside)" in line and "hero_vo.wav" in line, line)


def test_splice_keeps_audio():
    """The fused soundtrack must survive the RefMod latent splice.

    `visual_block` builds a silent block, so a wholesale replace threw the
    encoder's audio away -- the voice loaded, encoded, and vanished with no
    error anywhere. This is the regression test for that.
    """
    pixels = torch.zeros(5, 8, 8, 3)
    stored = M.visual_block(
        {"kind": "video", "latent": torch.ones(1, 24, 2, 4, 4)}, 1.0)
    ck("a stored video block starts silent", stored["ref_audio_t"] == 0)

    audio = torch.ones(1, 32, 2, 9)
    cond = [["t", {"minimax_refs": [{
        "kind": "video_audio", "latent_t": 2, "latent_h": 4, "latent_w": 4,
        "ref_audio_t": 9, "latent": torch.zeros(1, 24, 2, 4, 4),
        "audio_latent": audio}]}]]
    out = M.splice_blocks(cond, {}, {"ref_video_1": pixels}, {},
                          {id(pixels): stored})
    got = out[0][1]["minimax_refs"][0]
    ck("the spliced block stays video_audio", got["kind"] == "video_audio",
       str(got["kind"]))
    ck("the encoded soundtrack is carried across", got["audio_latent"] is audio)
    ck("its audio length comes with it", got["ref_audio_t"] == 9)
    ck("the stored latent still won the video half",
       bool((got["latent"] == 1).all()))


def test_stored_plan():
    """The header records which sources went in, not just their shape."""
    latent = torch.zeros(1, 24, 1, 8, 8)
    meta = M.metadata_for("m", "image", latent, "encode", "images", "1x8x8",
                          "encode", 0,
                          plan={"stills": ["a.png", "b.png"]})
    ck("a built mod records its sources",
       meta["mod_plan"]["stills"] == ["a.png", "b.png"])
    ck("the socket path records nothing rather than lying",
       M.metadata_for("m", "image", latent, "encode", "image", "1x8x8",
                      "encode", 0)["mod_plan"] == {})

    with tempfile.TemporaryDirectory() as tmp:
        M.save_latent(tmp, "probe", latent, meta)
        back = M.read_meta(os.path.join(tmp, "probe.safetensors"))
    ck("it survives the safetensors round trip",
       back["mod_plan"]["stills"] == ["a.png", "b.png"])
    ck("and parse_mod_plan accepts it straight back",
       M.parse_mod_plan(back["mod_plan"])[0] == ["a.png", "b.png"])

    voiced = M.metadata_for("m", "image", latent, "encode", "images", "1x8x8",
                            "encode", 0,
                            plan={"stills": ["a.png"], "voice": "v.wav"})
    ck("a mod that names a voice keeps it in the header",
       M.parse_mod_plan(voiced["mod_plan"])[2] == "v.wav")


def test_description():
    """The caption rides in the header and reaches the rail's listing."""
    latent = torch.zeros(1, 24, 1, 8, 8)
    meta = M.metadata_for("cap", "image", latent, "encode", "images",
                          "1x8x8", "encode", 0,
                          description="  a woman in a red coat  ")
    ck("the builder's description is written, trimmed",
       meta["description"] == "a woman in a red coat", meta["description"])
    ck("and a mod built without one still writes an empty field",
       M.metadata_for("m", "image", latent, "encode", "image", "1x8x8",
                      "encode", 0)["description"] == "")
    ck("a bundle falls back to its visual member's caption",
       M.description_of({"kind": "bundle", "description": "", "members": [
           {"kind": "audio", "description": "a voice"},
           {"kind": "video", "description": "a dancer"}]}) == "a dancer")
    ck("a header that is not a dict has no caption",
       M.description_of(None) == "")
    from safetensors.torch import save_file
    with tempfile.TemporaryDirectory() as tmp:
        M.save_latent(tmp, "cap", latent, meta)
        # Another builder's file: Luisa's layout, caption filled in.
        save_file({"latent": latent}, os.path.join(tmp, "other.safetensors"),
                  metadata={M.META_KEY: json.dumps(
                      {"_format_version": 4, "kind": "image",
                       "description": "an old man, grey beard"})})
        real = M.roots
        M.roots = lambda: [tmp]
        try:
            rows = {r["name"]: r for r in M.listing()}
        finally:
            M.roots = real
    ck("the listing carries each file's caption",
       rows["cap"]["description"] == "a woman in a red coat"
       and rows["other"]["description"] == "an old man, grey beard",
       repr(rows))


def test_video_subject():
    """A character mod is a video reference AND an identity.

    The builder fuses several stills into one clip, so refusing `subject` on a
    video row refused the feature. What has to hold is that the subject is
    defined against the slot the encoder actually received.
    """
    plan = R.parse_ref_plan(json.dumps({"refs": [
        {"tag": "hero", "source": "refmod", "file": "hero", "subject": 1},
        {"tag": "kitchen", "file": "kitchen.png", "retention": "reference"},
    ], "subjects": {"1": {"name": "Miyu", "locked": "dark hair"}}}))
    refs = plan["refs"]
    refs[0]["kind"] = "video"          # set by run() once the file is read
    prose = R.subject_prose(refs, plan["subjects"], {"hero": 1})
    ck("a video RefMod can be a subject",
       "<Subject 1> is Miyu, the person in <Video 1>." in prose, prose)
    ck("and it is not cited as a picture",
       "<Picture 1>: the layout" in prose and "Miyu, the person in <Picture" not in prose,
       prose)
    # The still keeps <Picture 1>: video refs never spent a picture ordinal,
    # and must not start now.
    ck("the still beside it keeps <Picture 1>",
       R.ordinals([r for r in refs if r.get("kind") != "video"])["kitchen"] == 1)
    off = R.subject_prose(refs, plan["subjects"], {})
    ck("a mod that is off this hop defines no subject",
       "subject_definitions:" not in off, off)


def test_subject_mask():
    """The backdrop is repainted from the subject on pixels, before encode."""
    # A red square on a white sheet: the backdrop must lose its white.
    px = torch.ones(1, 64, 64, 3)
    px[:, 16:48, 16:48] = torch.tensor([0.8, 0.1, 0.1])
    m = torch.zeros(1, 64, 64)
    m[:, 16:48, 16:48] = 1.0
    out = M.isolate_subject(px, m, 0.0)
    ck("the subject is left exactly as it was",
       torch.equal(out[:, 16:48, 16:48], px[:, 16:48, 16:48]))
    corner = out[0, 2, 2]
    ck("the backdrop takes the subject's colour, not the sheet's white",
       torch.allclose(corner, torch.tensor([0.8, 0.1, 0.1]), atol=0.02), repr(corner))
    ck("retention 1 changes nothing",
       torch.equal(M.isolate_subject(px, m, 1.0), px))
    mid = M.isolate_subject(px, m, 0.4)
    ck("retention in between keeps that much of the backdrop",
       torch.allclose(mid[0, 2, 2], 0.4 * px[0, 2, 2] + 0.6 * out[0, 2, 2], atol=1e-5))
    ck("an empty mask leaves the frame alone rather than inventing a colour",
       torch.equal(M.isolate_subject(px, torch.zeros(1, 64, 64), 0.0), px))
    clip = px.expand(3, -1, -1, -1).clone()
    ck("one mask covers every frame of a clip",
       M.isolate_subject(clip, m, 0.0).shape == clip.shape)
    fill = M.pull_push_fill(torch.rand(1, 37, 53, 3), m[:, :37, :53])
    ck("the fill copes with odd sizes", fill.shape == (1, 37, 53, 3))

    # A cutout unsure inside the subject: a grey patch, and a small hole.
    soft = torch.zeros(1, 200, 200)
    soft[:, 40:200, 40:160] = 1.0                 # a bust cut off by the frame
    soft[:, 120:170, 60:100] = 0.4                # the model was not sure here
    soft[:, 70:74, 90:94] = 0.0                   # a gap between strands
    soft[:, 100:160, 120:150] = 0.0               # a real pocket of backdrop
    soft[:, 40:200, 38:40] = 0.5                  # the rim a cutout leaves
    sheet = torch.ones(1, 200, 200, 3)            # a white sheet
    sheet[:, 40:200, 40:160] = 0.3                # a dark figure on it
    sheet[:, 60:66, 80:120] = 1.0                 # with a white collar
    sheet[:, 100:160, 120:150] = 1.0
    solid = M.refine_cutout(soft, sheet)
    ck("an unsure patch inside the subject becomes subject",
       float(solid[0, 140, 80]) == 1.0)
    ck("a small enclosed gap is part of the subject",
       float(solid[0, 72, 92]) == 1.0)
    ck("a large enclosed pocket stays backdrop",
       float(solid[0, 130, 135]) == 0.0)
    ck("the border-connected backdrop stays backdrop",
       float(solid[0, 5, 5]) == 0.0)
    ck("the sheet-coloured rim round the figure is let go",
       float(solid[0, 100, 38]) == 0.0, float(solid[0, 100, 38]))
    ck("a white collar the model was sure of is kept",
       float(solid[0, 62, 100]) == 1.0)
    room = torch.rand(1, 200, 200, 3)             # a photographed room
    ck("a busy backdrop is not colour-keyed",
       float(M.refine_cutout(soft, room)[0, 100, 38]) == 0.5)

    ck("a mask is stretched onto the canvas like the pixels are",
       M.mask_to_canvas(torch.ones(1, 90, 160), 320, 192).shape == (1, 192, 320))
    z = torch.zeros(1, 24, 1, 16, 16)
    meta = M.metadata_for("m", "image", z, "encode", "image", "1x16x16", "encode", 0,
                          mask={"source": "auto", "background_retention": 0.0})
    ck("the header records how the mod was masked",
       meta["subject_mask"]["source"] == "auto")
    ck("and an unmasked mod records nothing",
       M.metadata_for("m", "image", z, "encode", "image", "1x16x16", "encode",
                      0)["subject_mask"] == {})


def test_builder_mask():
    """The builder masks each source and refuses a batch that does not fit."""
    import importlib
    import types
    pkg = types.ModuleType("htc_pack")
    pkg.__path__ = [_PACK]
    sys.modules.setdefault("htc_pack", pkg)
    B = importlib.import_module("htc_pack.refmod_builder")
    BM = sys.modules["htc_pack.refmod"]

    class VAE:
        def encode(self, px):
            t = 1 + (px.shape[0] - 1) // 4
            return torch.ones(1, 24, t, px.shape[1] // 16, px.shape[2] // 16)

    seen = []
    real = BM.isolate_subject
    BM.isolate_subject = lambda f, m, r: (seen.append((tuple(m.shape), r)), real(f, m, r))[1]
    try:
        still = torch.rand(1, 256, 320, 3)
        name, = B.HTCRefModBuilder().build(
            VAE(), "m", "encode", images=still, save=False, build="start",
            mask=torch.ones(1, 128, 160), background_retention=0.25)
        ck("a wired mask reaches the pixels on the canvas, with its retention",
           seen == [((1, 256, 320), 0.25)], repr(seen))
        seen.clear()
        B.HTCRefModBuilder().build(VAE(), "m", "encode", images=still,
                                   save=False, build="start")
        ck("no mask, no masking", seen == [])
        try:
            B.HTCRefModBuilder().build(
                VAE(), "m", "encode", build="start", save=False,
                mod_plan=json.dumps({"stills": ["a.png", "b.png", "c.png"]}),
                mask=torch.ones(2, 64, 64))
            ck("a mask batch that fits no source count is refused", False)
        except ValueError as exc:
            ck("a mask batch that fits no source count is refused",
               "2 masks for 3 sources" in str(exc), str(exc))
    finally:
        BM.isolate_subject = real


if __name__ == "__main__":
    test_subject_mask()
    test_builder_mask()
    test_schedule()
    test_strength_and_file()
    test_kind_cap()
    test_mod_plan()
    test_stack()
    test_canvas()
    test_mod_voice()
    test_source_problem()
    test_row_voice()
    test_video_subject()
    test_splice_keeps_audio()
    test_stored_plan()
    test_description()
    print("check_refmod: ok")
