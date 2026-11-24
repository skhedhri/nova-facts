"""Daily YouTube Short: Gemini script -> fact/safety review -> render -> YouTube upload.

CONTENT_MODE=facts (default): "did you know" Shorts for a general audience, one series per weekday.
CONTENT_MODE=kids: the original short educational videos for ages 3-7 with a fixed mascot.
"""
import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

import render  # render.py: edge-tts + AI images + FFmpeg

# Tried in order; if Google retires one (HTTP 404) the next one is used.
# You can override with a GEMINI_MODELS env value, comma-separated.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.1-flash-lite,gemini-3.8-flash,gemini-4-hydrogen"
).split(",") if m.strip()]
# The reviewer tries the models in the opposite order, so a different model
# usually checks the script than the one that wrote it.
REVIEW_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_REVIEW_MODELS", ",".join(reversed(GEMINI_MODELS))
).split(",") if m.strip()]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

MODE = os.environ.get("CONTENT_MODE", "facts").strip().lower()
KIDS = MODE == "kids"
LANGUAGE = os.environ.get("LANGUAGE", "English")
VOICE = os.environ.get("VOICE", "en-US-AnaNeural" if KIDS else "en-US-AndrewNeural")
PRIVACY = os.environ.get("PRIVACY", "private")
SCENES = int(os.environ.get("SCENE_COUNT", "6" if KIDS else "8"))
SHORTS = os.environ.get("VIDEO_FORMAT", "shorts").lower() == "shorts"

IMAGE_STYLE = os.environ.get("IMAGE_STYLE") or (
    "3D animated kids cartoon, soft studio lighting, glossy pastel colors, big friendly eyes, "
    "clean simple background, high quality, sharp" if KIDS else
    "cinematic photo style, dramatic lighting, rich saturated colors, shallow depth of field, "
    "ultra detailed, sharp, realistic")

# Kids mode: ONE fixed mascot that appears in every video (this is what builds a brand).
MASCOT = os.environ.get(
    "MASCOT",
    "Pip, a small round fluffy yellow chick with big friendly eyes, a tiny red scarf and orange feet")

KIDS_CATEGORIES = [
    "Animals and their babies", "Colors and shapes", "Counting numbers 1 to 10",
    "Space and planets", "Bedtime story with a gentle ending", "Ocean life",
    "Fruits and vegetables", "Weather and seasons", "Vehicles and machines",
    "Dinosaurs", "Kindness and sharing", "The alphabet", "Farm animals",
    "Insects and bugs", "Healthy habits like brushing teeth", "Musical instruments",
]

# Facts mode: one recurring series per weekday (Monday = 0). Same series name every week
# gives the channel a recognisable structure.
SERIES = {
    0: ("Space Mysteries", "space, planets, stars, black holes, the universe"),
    1: ("Wild Animal Facts", "surprising true facts about animals and their abilities"),
    2: ("Ocean Secrets", "the deep sea, ocean animals and ocean science"),
    3: ("Science Is Wild", "everyday science, physics, chemistry and natural phenomena"),
    4: ("Human Body Wonders", "how the human body works; amazing facts only, never medical advice"),
    5: ("Ancient Mysteries", "ancient civilizations, buildings and inventions; no named people"),
    6: ("Inventions That Changed Everything", "inventions and technology and how they work"),
}


def TODAY():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def gemini_json(prompt, models=None):
    last = None
    for model in list(models or GEMINI_MODELS):
        low_thinking = True  # keep answers fast; dropped automatically if not accepted
        attempt = 0
        while attempt < 4:  # overloaded (503/429): retry with growing pauses
            cfg = {"responseMimeType": "application/json", "temperature": 1}
            if low_thinking:
                cfg["thinkingConfig"] = {"thinkingLevel": "low"}
            body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": cfg}).encode()
            try:
                req = urllib.request.Request(
                    f"{GEMINI_BASE}{model}:generateContent", data=body,
                    headers={"Content-Type": "application/json",
                             "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                return json.loads(text.replace("```json", "").replace("```", "").strip())
            except urllib.error.HTTPError as e:
                last = e
                print(f"Gemini {model} failed: HTTP {e.code}", flush=True)
                if e.code == 400 and low_thinking:
                    low_thinking = False  # retry same model without thinking setting
                    continue
                if e.code in (400, 403, 404):
                    break  # model unavailable for this key: next model
                attempt += 1
                time.sleep(15 * 2 ** attempt)  # 30s, 60s, 120s
            except Exception as e:
                last = e
                print(f"Gemini {model} failed: {e}", flush=True)
                attempt += 1
                time.sleep(10 * attempt)
    raise RuntimeError(f"Gemini failed on all models: {last}")


# ----------------------------------------------------------------------------- facts mode
def write_script_facts(series, focus, recent):
    return gemini_json(
        "You write viral, accurate 'did you know' YouTube Shorts for a general audience.\n"
        f"Language of the narration: {LANGUAGE}.\n"
        f"Series: {series} ({focus}).\n"
        "Choose ONE specific, surprising, TRUE and well-documented fact for this series that is "
        f"NOT in this list: {json.dumps(recent)}.\n"
        f"Write exactly {SCENES} scenes (about 40 seconds in total). "
        "Scene 1 is a HOOK of at most 10 words that makes people keep watching "
        "(a surprising claim or question, no greeting). "
        "Every narration is ONE short punchy sentence of at most 14 words, simple words, "
        "with curiosity rising scene after scene. "
        "The LAST scene gives the payoff and ends with a short question that invites viewers to comment. "
        "Only include claims you are certain are true and widely documented; no exaggerated numbers; "
        "if you are not sure, choose a different fact.\n"
        "Rules: nothing graphic, violent, sexual, political, hateful or commercial; no brands; "
        "no named living people; no medical advice.\n"
        "Image prompts: English, VERTICAL portrait composition, subject centered, concrete and "
        "self-contained, describe ONLY what is shown in that scene, no text in the picture.\n"
        'Return ONLY valid JSON: {"topic":"","key_fact":"the single central claim, one sentence",'
        '"title":"curiosity title under 55 characters with one emoji, never a false promise",'
        '"description":"2 sentences","tags":["8 to 12 tags"],'
        '"scenes":[{"narration":"","image_prompt":""}]}'
    )


def review_facts(script):
    verdict = gemini_json(
        "You are a strict fact-checker and safety reviewer for short educational videos for a "
        "general audience. It is UNSAFE if key_fact or ANY narration claim is false, exaggerated, "
        "disputed or not well documented, or if anything is graphic, sexual, hateful, political, "
        "commercial, brand-related, gives medical advice, or names a living person. "
        "A closing question that invites comments is fine. "
        'Return ONLY JSON: {"safe":true or false,"reason":"short reason"}.\n\nSCRIPT:\n'
        + json.dumps({"key_fact": script.get("key_fact", ""), "title": script["title"],
                      "scenes": script["scenes"]}),
        models=REVIEW_MODELS)
    return verdict.get("safe") is True, verdict.get("reason", "")


# ----------------------------------------------------------------------------- kids mode
def write_script_kids(category, recent):
    style = (
        "This is a vertical YouTube Short of about 40 seconds. Scene 1 is a curiosity HOOK that grabs "
        "attention in the first 3 seconds (for example a surprising question or 'Did you know...?'). "
        "Every narration is ONE or TWO very short, energetic sentences (maximum 14 words each). "
        "The LAST scene ends with a fun question for the kids and a friendly goodbye like 'See you tomorrow for another fun fact!'. "
        "Never ask kids to follow, subscribe, like, click or visit anything. "
        "Image prompts must describe a VERTICAL portrait composition with the subject centered, bold bright colors."
        if SHORTS else
        "Each narration is 2 or 3 short, simple, cheerful sentences a child can follow."
    )
    return gemini_json(
        f"You write short, safe, educational videos for children aged 3 to 7.\n"
        f"Language of the narration: {LANGUAGE}.\nCategory: {category}.\n"
        f"The recurring main character is: {MASCOT}. The mascot appears in every scene and in the story.\n"
        f"Choose ONE fresh, specific topic in this category that is NOT in this list: "
        f"{json.dumps(recent)}.\n"
        f"Write exactly {SCENES} scenes. {style}\n"
        f"Rules: nothing scary, violent, romantic, commercial or branded; no real people; "
        f"no existing cartoon characters; no medical advice.\n"
        'Return ONLY valid JSON: {"topic":"","title":"catchy title under 60 characters with one emoji",'
        '"description":"2-3 sentences for parents","tags":["8 to 12 tags"],'
        '"scenes":[{"narration":"","image_prompt":"English, describe ONLY what happens in this scene: '
        'action, pose and setting, vertical composition, subject centered"}]}'
    )


def review_kids(script):
    verdict = gemini_json(
        "You are a strict child-safety reviewer for videos aimed at ages 3 to 7. "
        "A friendly goodbye such as 'see you tomorrow' is fine and is NOT commercial. "
        "It is UNSAFE if anything is scary, violent, sexual, hateful, political, "
        "commercial, brand-related, medical advice, mentions a real person or an "
        "existing copyrighted character, gives false facts, or is not age-appropriate. "
        'Return ONLY JSON: {"safe":true or false,"reason":"short reason"}.\n\nSCRIPT:\n'
        + json.dumps({"title": script["title"], "scenes": script["scenes"]}),
        models=REVIEW_MODELS)
    return verdict.get("safe") is True, verdict.get("reason", "")


# ----------------------------------------------------------------------------- upload
def youtube_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds)


def upload(path, script, series=None):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = youtube_service()

    if KIDS:
        description = script["description"] + "\n\nMade with AI for kids."
        hashtags = "#Shorts #kids #learning"
    else:
        tag = "".join(ch for ch in (series or "facts").title() if ch.isalnum())
        description = (script["description"]
                       + (f"\n\nSeries: {series}" if series else "")
                       + "\n\nThe pictures and the voice in this video are AI-generated.")
        hashtags = f"#Shorts #facts #didyouknow #{tag}"
    if SHORTS:
        description += "\n\n" + hashtags

    def make_body(synthetic):
        status = {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": KIDS}
        if synthetic:
            status["containsSyntheticMedia"] = True  # "altered or synthetic content" label
        return {
            "snippet": {
                "title": (script["title"][:88] + (" #Shorts" if SHORTS else "")),
                "description": description,
                "tags": (script["tags"][:11] + (["shorts"] if SHORTS else [])),
                "categoryId": "27",
            },
            "status": status,
        }

    def send(body):
        request = yt.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(path, mimetype="video/mp4", resumable=True))
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response["id"]

    try:
        return send(make_body(synthetic=not KIDS))
    except HttpError as e:
        if not KIDS and e.resp.status == 400:
            print("Upload rejected with the synthetic-media flag; retrying without it.", flush=True)
            return send(make_body(synthetic=False))
        raise


# ----------------------------------------------------------------------------- main
def main():
    history = json.load(open("history.json"))
    recent = [h["topic"] for h in history[-80:]]

    weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
    series, focus = SERIES[weekday]
    if os.environ.get("SERIES"):  # manual override for a test run
        series, focus = os.environ["SERIES"], os.environ["SERIES"]

    for attempt in range(3):
        if KIDS:
            category = random.choice(KIDS_CATEGORIES)
            script = write_script_kids(category, recent)
        else:
            category = series
            script = write_script_facts(series, focus, recent)
        script["scenes"] = [
            {"narration": str(s["narration"]), "image_prompt": str(s["image_prompt"])}
            for s in script["scenes"]
        ]
        if len(script["scenes"]) < 3:
            continue
        for sc in script["scenes"]:
            if KIDS:
                sc["image_prompt"] = (f"{sc['image_prompt']}. Main character in every scene: {MASCOT}. "
                                      f"Style: {IMAGE_STYLE}. No text, no letters.")
            else:
                sc["image_prompt"] = (f"{sc['image_prompt']}. Style: {IMAGE_STYLE}. "
                                      "No text, no letters, no logos, no watermark.")
        safe, reason = (review_kids if KIDS else review_facts)(script)
        topic = str(script.get("topic", category))
        if not safe:
            print(f"Rejected '{topic}': {reason}", flush=True)
            history.append({"topic": topic, "status": "rejected", "reason": reason, "date": TODAY()})
            recent.append(topic)
            continue

        print(f"Rendering '{topic}' ({series if not KIDS else 'kids'})...", flush=True)
        video = render.render({"voice": VOICE, "scenes": script["scenes"]})
        with open("video.mp4", "wb") as f:
            f.write(video)
        video_id = upload("video.mp4", script, None if KIDS else series)
        print(f"Uploaded: https://youtu.be/{video_id}", flush=True)
        history.append({"topic": topic, "status": "uploaded", "videoId": video_id,
                        "title": script["title"], "series": None if KIDS else series,
                        "kind": "daily", "date": TODAY()})
        break
    else:
        print("No safe script after 3 tries; nothing uploaded today.", flush=True)

    json.dump(history[-300:], open("history.json", "w"), indent=2)


if __name__ == "__main__":
    sys.exit(main())
