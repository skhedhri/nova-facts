"""
Render worker: turns a list of scenes into an MP4.

POST /render  {"title": str, "voice": str, "scenes": [{"narration": str, "image_prompt": str}]}
  -> returns an MP4 (video/mp4)

Per scene: Edge TTS narration (free) + AI image from Pollinations (free, no key)
+ slow zoom effect, then all scenes are concatenated. Optional background music
is mixed in from any .mp3 files found in /music.
"""
import base64
import glob
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SHORTS = os.environ.get("VIDEO_FORMAT", "shorts").lower() == "shorts"
W, H = (720, 1280) if SHORTS else (1280, 720)   # vertical 9:16 for Shorts
FPS = 30
VOICE_RATE = os.environ.get("VOICE_RATE", "+0%")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
MAX_SCENES = 20
MAX_NARRATION = 700


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ])
    return float(out.strip())


POLLINATIONS_KEY = os.environ.get("POLLINATIONS_API_KEY", "").strip()
_dead_endpoints = set()  # endpoints that answered 401/402/403: skipped for the rest of the run


CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()


def fetch_cloudflare(prompt, dest, seed):
    """FLUX.1 [schnell] on Cloudflare Workers AI (free daily allowance). Returns True on success."""
    if not (CF_ACCOUNT and CF_TOKEN) or "cloudflare" in _dead_endpoints:
        return False
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    # Only documented inputs: prompt (max 2048 chars) and steps (max 8).
    body = json.dumps({"prompt": prompt[:2000], "steps": 6}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            img = base64.b64decode(data["result"]["image"])
            if len(img) > 5000:
                with open(dest, "wb") as f:
                    f.write(img)
                return True
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                detail = ""
            print(f"image [cloudflare] attempt {attempt + 1} failed: HTTP {e.code} {detail}", flush=True)
            if "daily free allocation" in detail.lower() or "4006" in detail:
                _dead_endpoints.add("cloudflare")  # free allowance used up for today
                return False
            if e.code in (401, 403):
                _dead_endpoints.add("cloudflare")  # wrong token / no permission
                return False
            if e.code == 429:
                time.sleep(15)
        except Exception as e:
            print(f"image [cloudflare] attempt {attempt + 1} failed: {e}", flush=True)
        time.sleep(4 * (attempt + 1))
    return False


def fetch_image(prompt, dest, seed):
    """Cloudflare FLUX first, then Pollinations as a backup. Returns True on success."""
    if fetch_cloudflare(prompt, dest, seed):
        return True
    q = urllib.parse.quote(prompt)
    params = f"width={W}&height={H}&seed={seed}&nologo=true&private=true"
    endpoints = []
    if POLLINATIONS_KEY:
        endpoints.append(("keyed", f"https://gen.pollinations.ai/image/{q}?model=flux&{params}",
                          {"Authorization": f"Bearer {POLLINATIONS_KEY}"}))
    endpoints.append(("keyless", f"https://gen.pollinations.ai/image/{q}?model=flux&{params}", {}))
    endpoints.append(("legacy", f"https://image.pollinations.ai/prompt/{q}?{params}", {}))

    for name, url, extra in endpoints:
        if name in _dead_endpoints:
            continue
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **extra})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                if len(data) > 5000:
                    with open(dest, "wb") as f:
                        f.write(data)
                    return True
            except urllib.error.HTTPError as e:
                print(f"image [{name}] attempt {attempt + 1} failed: HTTP {e.code}", flush=True)
                if e.code in (401, 402, 403):
                    _dead_endpoints.add(name)  # needs a key / no credit: do not retry
                    break
                if e.code == 429:
                    time.sleep(15)
            except Exception as e:
                print(f"image [{name}] attempt {attempt + 1} failed: {e}", flush=True)
            time.sleep(4 * (attempt + 1))
    return False


def fallback_image(dest):
    """Plain pastel background if the image service is down."""
    color = random.choice(["0x8ecae6", "0xffb703", "0xb5e48c", "0xffafcc", "0xcdb4db"])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}",
         "-frames:v", "1", dest])


def tts(text, voice, dest, workdir, idx):
    txt = os.path.join(workdir, f"narration_{idx}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    last = None
    for attempt in range(3):
        try:
            run(["edge-tts", "--voice", voice, "--file", txt,
                 f"--rate={VOICE_RATE}", "--write-media", dest])
            return
        except subprocess.CalledProcessError as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"TTS failed: {last.stderr.decode(errors='ignore')[:300] if last else ''}")


TRANSITION = 0.3   # seconds of cross-dissolve between scenes
PAD = 0.8          # pause after each narration (must be >= 2 * TRANSITION + 0.1)

# Camera moves with a gentle "bounce" (sin wobble) so still pictures feel alive.
# F = frames, on = frame number.
MOVES = [
    ("zoom in",   "1.06+0.10*on/{F}+0.012*sin(on/4)",
                  "iw/2-(iw/zoom/2)+iw*0.012*sin(on/11)", "ih/2-(ih/zoom/2)+ih*0.010*sin(on/7)"),
    ("pan right", "1.14+0.012*sin(on/4)",
                  "(iw-iw/zoom)*on/{F}", "ih/2-(ih/zoom/2)+ih*0.012*sin(on/6)"),
    ("zoom out",  "1.16-0.10*on/{F}+0.012*sin(on/4)",
                  "iw/2-(iw/zoom/2)+iw*0.012*sin(on/9)", "ih/2-(ih/zoom/2)+ih*0.010*sin(on/8)"),
    ("pan left",  "1.14+0.012*sin(on/4)",
                  "(iw-iw/zoom)*(1-on/{F})", "ih/2-(ih/zoom/2)+ih*0.012*sin(on/6)"),
]


def make_clip(img, out, dur, move_idx):
    """Silent video clip of one image with a slow camera move."""
    frames = int(dur * FPS) + 1
    _, z, x, y = MOVES[move_idx % len(MOVES)]
    z, x, y = (t.replace("{F}", str(frames)) for t in (z, x, y))
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
        f"crop={W * 2}:{H * 2},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS), out,
    ])


TRANSITION_STYLES = ["slideleft", "slideup", "circleopen", "wipeleft",
                     "smoothleft", "fadewhite", "radial", "slideright"]
CAPTIONS = os.environ.get("CAPTIONS", "on").lower() != "off"
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def caption_filters(texts, starts, durs, workdir):
    """Big word-group captions that pop in (one drawtext per group)."""
    font = find_font()
    if not (CAPTIONS and font):
        return []
    size = 60 if SHORTS else 52
    filters = []
    for i, text in enumerate(texts):
        clean = re.sub(r"[^\w\s'.,!?-]", "", text.replace("\u2019", "'"))
        groups, cur = [], []
        for w in clean.split():
            if cur and (len(cur) >= 2 or len(" ".join(cur + [w])) > 14):
                groups.append(" ".join(cur))
                cur = []
            cur.append(w)
        if cur:
            groups.append(" ".join(cur))
        total = sum(len(g) + 2 for g in groups) or 1
        acc = 0
        for j, g in enumerate(groups):
            t0 = starts[i] + durs[i] * acc / total
            acc += len(g) + 2
            t1 = starts[i] + durs[i] * acc / total
            if j == len(groups) - 1:
                t1 = starts[i] + durs[i] + 0.2
            path = os.path.join(workdir, f"cap_{i}_{j}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(g)
            filters.append(
                f"drawtext=fontfile='{font}':textfile='{path}':expansion=none:"
                f"fontsize='{size}*(1+0.3*max(0,1-(t-{t0:.3f})*7))':"
                f"fontcolor=white:borderw=6:bordercolor=black:"
                f"x=(w-text_w)/2:y=h*0.72:enable='between(t,{t0:.3f},{t1:.3f})'"
            )
    return filters


def _assemble(clips, audios, out, texts=None, use_captions=True, simple_transitions=False):
    """Dynamic transitions + pop-in captions; each narration starts when its scene is visible."""
    n = len(clips)
    durs = [duration(c) for c in clips]
    adurs = [duration(a) for a in audios]
    workdir = os.path.dirname(out)
    inputs, filters = [], []
    for c in clips:
        inputs += ["-i", c]
    for a in audios:
        inputs += ["-i", a]

    # video: chain of xfade transitions (a different style at each cut)
    if simple_transitions:
        styles = ["fade"] * max(n - 1, 0)
    else:
        styles = random.sample(TRANSITION_STYLES, k=min(len(TRANSITION_STYLES), max(n - 1, 0)))
        while len(styles) < n - 1:
            styles.append(random.choice(TRANSITION_STYLES))
    if n == 1:
        vlabel, total = "0:v", durs[0]
    else:
        prev, acc = "0:v", durs[0]
        for k in range(1, n):
            offset = acc - TRANSITION
            lab = f"v{k}"
            filters.append(
                f"[{prev}][{k}:v]xfade=transition={styles[k - 1]}:"
                f"duration={TRANSITION}:offset={offset:.3f}[{lab}]")
            prev = lab
            acc = offset + durs[k]
        vlabel, total = prev, acc

    # audio: each narration starts once its scene is fully visible
    scene_start, starts, labels = 0.0, [], []
    for i in range(n):
        if i > 0:
            scene_start += durs[i - 1] - TRANSITION
        begin = scene_start + (TRANSITION if i > 0 else 0.3)
        starts.append(begin)
        lab = f"s{i}"
        filters.append(
            f"[{n + i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"adelay={int(begin * 1000)}:all=1[{lab}]")
        labels.append(f"[{lab}]")
    filters.append("".join(labels) + f"amix=inputs={n}:duration=longest:normalize=0[aout]")

    # captions on top of the finished video
    caps = caption_filters(texts, starts, adurs, workdir) if (texts and use_captions) else []
    if caps:
        filters.append(f"[{vlabel}]" + ",".join(caps) + "[vout]")
        vmap = "[vout]"
    else:
        vmap = f"[{vlabel}]" if vlabel != "0:v" else "0:v"

    try:
        run([
            "ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
            "-map", vmap, "-map", "[aout]",
            "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", out,
        ])
    except subprocess.CalledProcessError as e:
        print("FFMPEG ERROR (last lines):\n" + (e.stderr or b"").decode("utf-8", "ignore")[-1500:],
              flush=True)
        raise


def assemble(clips, audios, out, texts=None):
    """Try the full version first; fall back to simpler ones so a video is always produced."""
    attempts = [(True, False), (False, False), (False, True)]  # (captions, simple transitions)
    last = None
    for captions, simple in attempts:
        try:
            _assemble(clips, audios, out, texts, captions, simple)
            return
        except subprocess.CalledProcessError as e:
            last = e
            print(f"Assembly failed (captions={captions}, simple transitions={simple}); "
                  "trying a simpler version...", flush=True)
    raise last


def render(payload):
    scenes = payload.get("scenes") or []
    voice = payload.get("voice") or "en-US-AnaNeural"
    if not scenes or len(scenes) > MAX_SCENES:
        raise ValueError(f"scenes must contain 1-{MAX_SCENES} items")

    workdir = tempfile.mkdtemp(prefix="render_")
    try:
        clips, audios, texts = [], [], []
        fallbacks = 0
        move0 = random.randint(0, 3)
        base_seed = random.randint(1, 10_000)
        for i, sc in enumerate(scenes):
            narration = str(sc.get("narration", "")).strip()[:MAX_NARRATION]
            prompt = str(sc.get("image_prompt", "")).strip() or narration
            if not narration:
                raise ValueError(f"scene {i} has no narration")

            audio = os.path.join(workdir, f"a_{i}.mp3")
            img = os.path.join(workdir, f"i_{i}.jpg")
            clip = os.path.join(workdir, f"c_{i}.mp4")

            tts(narration, voice, audio, workdir, i)
            if not fetch_image(prompt, img, base_seed + i):
                fallbacks += 1
                fallback_image(img)
            make_clip(img, clip, duration(audio) + PAD, move0 + i)
            clips.append(clip)
            audios.append(audio)
            texts.append(narration)
            print(f"scene {i + 1}/{len(scenes)} done", flush=True)

        if fallbacks > len(scenes) // 2:
            raise RuntimeError(
                f"Image service unavailable ({fallbacks}/{len(scenes)} images failed). "
                "Not uploading a video without pictures. Add a POLLINATIONS_API_KEY secret.")

        joined = os.path.join(workdir, "joined.mp4")
        assemble(clips, audios, joined, texts)

        final = joined
        tracks = glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))
        music, level = None, 0.12
        if tracks:
            music = random.choice(tracks)
        elif os.environ.get("AMBIENT_MUSIC", "on").lower() != "off":
            try:
                music = os.path.join(workdir, "ambient.wav")
                make_ambient(music, duration(joined) + 1)
                level = 0.10
            except Exception as e:
                print(f"Ambient music failed, continuing without music: {e}", flush=True)
                music = None
        if music:
            final = os.path.join(workdir, "final.mp4")
            run([
                "ffmpeg", "-y", "-i", joined, "-stream_loop", "-1", "-i", music,
                "-filter_complex",
                f"[1:a]volume={level}[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", final,
            ])

        with open(final, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def make_ambient(dest, seconds):
    """Soft royalty-free background pad made with ffmpeg (used when music/ has no .mp3)."""
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=f=110", "-f", "lavfi", "-i", "sine=f=220",
        "-f", "lavfi", "-i", "sine=f=277.18", "-f", "lavfi", "-i", "sine=f=329.63",
        "-filter_complex",
        "[0][1][2][3]amix=inputs=4:normalize=0,tremolo=f=0.2:d=0.35,lowpass=f=1100,"
        "aecho=0.8:0.6:70:0.35,volume=0.9",
        "-t", f"{seconds:.2f}", dest,
    ])


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        if self.path != "/render":
            return self._send(404, b"{}", "application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            video = render(payload)
            self._send(200, video, "video/mp4")
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")


if __name__ == "__main__":
    print("render worker listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
