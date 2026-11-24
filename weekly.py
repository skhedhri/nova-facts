"""Weekly long video (about 6 minutes): a "Top 10 facts" recap of the week's Shorts.

Run by .github/workflows/weekly.yml. The channel needs long videos to build watch hours.
"""
import datetime
import glob
import json
import os
import random
import re
import sys
import tempfile

os.environ["VIDEO_FORMAT"] = "landscape"   # must be set BEFORE main / render are imported

import main      # noqa: E402
import render    # noqa: E402

SERIES_NAME = "Weekly Top 10"
FACTS = 10
CHUNK = 8                                   # scenes rendered per piece (render.py takes at most 20)
MIN_SCENES, MAX_SCENES_TOTAL = 24, 40


# --------------------------------------------------------------------------- script
def covered_this_week(history):
    """Topics of the Shorts uploaded in the last 7 days (or the last 7 uploads if dates are missing)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    daily = [h for h in history if h.get("status") == "uploaded" and h.get("kind") != "weekly"]
    recent = []
    for h in daily:
        try:
            if (today - datetime.date.fromisoformat(h.get("date", ""))).days <= 7:
                recent.append(h["topic"])
        except ValueError:
            pass
    if len(recent) < 3:
        recent = [h["topic"] for h in daily][-7:]
    return recent[-7:]


def write_script(covered, older):
    return main.gemini_json(
        "You write a YouTube video of 5 to 7 minutes for a general audience: a 'Top 10 amazing facts' "
        f"recap. Language of the narration: {main.LANGUAGE}.\n"
        f"This week the channel's Shorts covered these topics: {json.dumps(covered)}.\n"
        "Include each of those topics as one of the 10 facts (retold with one extra TRUE detail), then add "
        f"NEW, surprising, true and well-documented facts to reach exactly {FACTS} facts. "
        f"Do NOT use any of these older topics: {json.dumps(older)}.\n"
        "Structure, exactly 32 scenes:\n"
        " - Scene 1: intro hook of 20 to 30 words that teases the most surprising fact (no greeting).\n"
        " - For each of the 10 facts EXACTLY 3 scenes: (a) 'Number N.' plus a hook sentence of 15 to 25 words; "
        "(b) the explanation in 25 to 35 words; (c) the wow detail or a comparison in 25 to 35 words.\n"
        " - Scene 32: outro of 25 to 35 words: a quick recap, a question that invites viewers to comment, "
        "and 'follow for a new amazing fact every day'.\n"
        "Simple words, short sentences, curiosity rising from fact to fact. "
        "Only include claims you are certain are true and widely documented; no exaggerated numbers; "
        "if unsure, choose another fact.\n"
        "Rules: nothing graphic, violent, sexual, political, hateful or commercial; no brands; "
        "no named living people; no medical advice.\n"
        "Image prompts: English, HORIZONTAL widescreen 16:9, concrete and self-contained, describe ONLY "
        "what is shown in that scene, no text in the picture.\n"
        'Return ONLY valid JSON: {"title":"under 70 characters, starts with a number, mentions one or two '
        'of the facts, at most one emoji, never a false promise","description":"2 sentences",'
        '"facts":["the 10 facts, one short sentence each"],"tags":["10 to 12 tags"],'
        '"scenes":[{"narration":"","image_prompt":""}]}'
    )


# --------------------------------------------------------------------------- video
def add_music(joined, out, music_dir, ambient_setting):
    """Mix background music once over the whole video (mp3 from music/, else a generated soft pad)."""
    tracks = glob.glob(os.path.join(music_dir, "*.mp3"))
    music, level = None, 0.12
    if tracks:
        music = random.choice(tracks)
    elif ambient_setting.lower() != "off":
        try:
            music = os.path.join(os.path.dirname(out), "ambient.wav")
            render.make_ambient(music, render.duration(joined) + 1)
            level = 0.10
        except Exception as e:
            print(f"Ambient music failed, continuing without music: {e}", flush=True)
            music = None
    if not music:
        return joined
    render.run([
        "ffmpeg", "-y", "-i", joined, "-stream_loop", "-1", "-i", music,
        "-filter_complex",
        f"[1:a]volume={level}[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", out,
    ])
    return out


def render_long(scenes, voice, workdir):
    orig_dir = render.MUSIC_DIR
    orig_ambient = os.environ.get("AMBIENT_MUSIC", "on")
    render.MUSIC_DIR = os.path.join(workdir, "no-music")   # pieces get no music; it is added at the end
    os.environ["AMBIENT_MUSIC"] = "off"
    parts = []
    try:
        for k in range(0, len(scenes), CHUNK):
            print(f"Rendering scenes {k + 1}-{min(k + CHUNK, len(scenes))} of {len(scenes)}...", flush=True)
            data = render.render({"voice": voice, "scenes": scenes[k:k + CHUNK]})
            p = os.path.join(workdir, f"part_{k // CHUNK:02d}.mp4")
            with open(p, "wb") as f:
                f.write(data)
            parts.append(p)
    finally:
        render.MUSIC_DIR = orig_dir
        os.environ["AMBIENT_MUSIC"] = orig_ambient

    listing = os.path.join(workdir, "parts.txt")
    with open(listing, "w") as f:
        f.write("".join(f"file '{p}'\n" for p in parts))
    joined = os.path.join(workdir, "joined.mp4")
    render.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", joined,
    ])
    return add_music(joined, os.path.join(workdir, "final.mp4"), orig_dir, orig_ambient)


def make_thumbnail(video, out):
    """Frame from the intro + big text. Returns the path, or None if it could not be made."""
    font = render.find_font()
    if not font:
        return None
    line2 = "THIS WEEK"
    vf = (
        "scale=1280:720,drawbox=x=0:y=ih*0.55:w=iw:h=ih*0.45:color=black@0.60:t=fill,"
        f"drawtext=fontfile={font}:text='TOP 10 FACTS':fontcolor=white:fontsize=120:borderw=5:"
        "bordercolor=black:x=(w-text_w)/2:y=h*0.58,"
        f"drawtext=fontfile={font}:text='{line2}':fontcolor=0x00E5FF:fontsize=70:borderw=4:"
        "bordercolor=black:x=(w-text_w)/2:y=h*0.80"
    )
    try:
        render.run(["ffmpeg", "-y", "-ss", "1.5", "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "3", out])
        return out
    except Exception as e:
        print(f"Thumbnail failed: {e}", flush=True)
        return None


def set_thumbnail(video_id, path):
    """Custom thumbnails need a verified YouTube channel; failure is not fatal."""
    try:
        from googleapiclient.http import MediaFileUpload
        main.youtube_service().thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(path, mimetype="image/jpeg")).execute()
        print("Custom thumbnail set.", flush=True)
    except Exception as e:
        print(f"Could not set the thumbnail (channel may need verification): {e}", flush=True)


# --------------------------------------------------------------------------- run
def run():
    history = json.load(open("history.json"))
    covered = covered_this_week(history)
    older = [h["topic"] for h in history[-80:]]
    today = main.TODAY()

    script = scenes = None
    for attempt in range(3):
        draft = write_script(covered, older)
        drafted = [{"narration": str(s["narration"]), "image_prompt": str(s["image_prompt"])}
                   for s in draft.get("scenes", [])]
        if not (MIN_SCENES <= len(drafted) <= MAX_SCENES_TOTAL):
            print(f"Draft has {len(drafted)} scenes; trying again.", flush=True)
            continue
        for sc in drafted:
            sc["image_prompt"] = (f"{sc['image_prompt']}. Style: {main.IMAGE_STYLE}. "
                                  "No text, no letters, no logos, no watermark.")
        facts = [str(x) for x in draft.get("facts", [])][:FACTS]
        safe, reason = main.review_facts({"key_fact": " | ".join(facts), "title": draft["title"],
                                          "scenes": drafted})
        if not safe:
            print(f"Weekly draft rejected: {reason}", flush=True)
            history.append({"topic": "weekly draft", "status": "rejected", "reason": reason,
                            "kind": "weekly", "date": today})
            continue
        script, scenes = draft, drafted
        break

    if not script:
        print("No safe weekly script after 3 tries; nothing uploaded this week.", flush=True)
        json.dump(history[-300:], open("history.json", "w"), indent=2)
        return

    facts = [str(x) for x in script.get("facts", [])][:FACTS]
    tag = "".join(ch for ch in SERIES_NAME if ch.isalnum())
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1))
    script["description"] = (script["description"]
                             + (f"\n\nIn this video:\n{numbered}" if numbered else "")
                             + f"\n\n#facts #didyouknow #{tag}")
    script["tags"] = [str(t) for t in script.get("tags", [])] or ["facts", "did you know"]

    workdir = tempfile.mkdtemp(prefix="weekly_")
    final = render_long(scenes, main.VOICE, workdir)
    print(f"Video ready: {render.duration(final):.0f} seconds.", flush=True)

    video_id = main.upload(final, script, SERIES_NAME)
    print(f"Uploaded: https://youtu.be/{video_id}", flush=True)
    thumb = make_thumbnail(final, os.path.join(workdir, "thumb.jpg"))
    if thumb:
        set_thumbnail(video_id, thumb)

    history.append({"topic": f"WEEKLY: {script['title']}", "status": "uploaded", "videoId": video_id,
                    "title": script["title"], "series": SERIES_NAME, "kind": "weekly", "date": today})
    for f in facts:  # remember the facts so the daily Shorts do not repeat them
        history.append({"topic": f[:140], "status": "covered_in_weekly", "date": today})
    json.dump(history[-300:], open("history.json", "w"), indent=2)


if __name__ == "__main__":
    sys.exit(run())
