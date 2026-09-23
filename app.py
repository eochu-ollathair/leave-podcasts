#!/usr/bin/env python3
# READ FIRST: /root/app/leave-podcasts/PROJECT_NOTES.md
"""Read podcast speech and send a three-line morning report."""

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from defusedxml import ElementTree as ET
from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file


ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PODCAST_DATA_DIR", str(ROOT / "data")))
BASE = os.environ.get("PODCAST_BASE", "").rstrip("/")
SETTINGS = DATA / "settings.json"
LAST = DATA / "last_report.json"
DELIVERY = DATA / "last_delivery.json"
SENT_EPISODES = DATA / "sent_episodes.json"
SOURCE_FILE = ROOT / "dist" / "leave-podcasts-source.zip"
MODEL_URL = os.environ.get("PODCAST_MODEL_URL", "")
MODEL_NAME = os.environ.get("PODCAST_MODEL", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
app = Flask(__name__)
job_lock = threading.Lock()
job = {"running": False, "stage": "Ready", "error": None, "kind": None}
whisper_model = None


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def defaults():
    return {"shows": [], "lines": 3, "angle": "", "enabled": False,
            "time": "08:00", "timezone": "Europe/Dublin"}


def settings():
    if not SETTINGS.exists():
        save_json(SETTINGS, defaults())
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("Choices were not understood")
    raw = value.get("shows", [])
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("Choose up to 20 podcasts")
    shows, seen = [], set()
    for item in raw:
        name = str(item.get("name", "")).strip()[:100]
        feed = str(item.get("feed", "")).strip()[:1200]
        if not name or urlparse(feed).scheme not in ("http", "https") or not urlparse(feed).netloc:
            raise ValueError("Each podcast needs a name and a link to its episodes")
        count = int(item.get("count", 1))
        if not 1 <= count <= 3:
            raise ValueError("Choose 1 to 3 episodes per podcast")
        if feed not in seen:
            shows.append({"name": name, "feed": feed, "count": count})
            seen.add(feed)
    send_time = str(value.get("time", "08:00"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Choose a valid morning time")
    tz = str(value.get("timezone", "Europe/Dublin")).strip()[:80]
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValueError("Choose a valid time zone") from None
    try:
        lines = int(value.get("lines", 3))
    except (TypeError, ValueError):
        raise ValueError("Choose a whole number of report lines") from None
    if not 1 <= lines <= 20:
        raise ValueError("Choose 1 to 20 report lines")
    return {"shows": shows, "lines": lines, "angle": str(value.get("angle", "")).strip()[:400],
            "enabled": bool(value.get("enabled", False)), "time": send_time, "timezone": tz}


def opening_key():
    key = os.environ.get("PODCAST_ACCESS_KEY", "")
    if key:
        return key
    credentials = os.environ.get("PODCAST_CREDENTIALS_FILE", "")
    if credentials:
        path = Path(credentials)
        path.touch(mode=0o600, exist_ok=True)
        with path.open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            for line in stream:
                if line.startswith("Leave the Podcasts access key: "):
                    saved = line.split(": ", 1)[1].strip()
                    if saved:
                        return saved
            saved = secrets.token_urlsafe(32)
            stream.seek(0, os.SEEK_END)
            stream.write("\nLeave the Podcasts access key: " + saved + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return saved
    path = DATA / "access-key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def authorised():
    return secrets.compare_digest(request.cookies.get("podcast_access", ""), opening_key())


def need_access():
    if not authorised():
        abort(403)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def search_shows(query):
    response = requests.get("https://itunes.apple.com/search",
                            params={"term": query, "media": "podcast", "entity": "podcast", "limit": 12},
                            timeout=20)
    response.raise_for_status()
    return [{"name": x.get("collectionName", ""), "maker": x.get("artistName", ""),
             "feed": x.get("feedUrl", "")} for x in response.json().get("results", [])
            if x.get("feedUrl")]


def child_text(node, name):
    child = node.find(name)
    return clean_text(child.text if child is not None else "")


def read_feed(show):
    response = requests.get(show["feed"], timeout=30, headers={"User-Agent": "LeavePodcasts/0.1"})
    response.raise_for_status()
    if len(response.content) > 8_000_000:
        raise RuntimeError("Podcast feed is too large")
    root = ET.fromstring(response.content)
    episodes = []
    for item in root.findall(".//item")[:100]:
        title = child_text(item, "title")
        date_text = child_text(item, "pubDate")
        try:
            published = parsedate_to_datetime(date_text).astimezone(timezone.utc).date().isoformat()
        except Exception:
            continue
        audio = item.find("enclosure")
        transcripts = []
        for element in item.iter():
            if element.tag.rsplit("}", 1)[-1] == "transcript" and element.attrib.get("url"):
                transcripts.append({"url": element.attrib["url"],
                                    "type": element.attrib.get("type", "")})
        if title and (transcripts or audio is not None):
            episodes.append({"show": show["name"], "title": title, "published": published,
                             "url": child_text(item, "link") or show["feed"],
                             "guid": child_text(item, "guid") or title + published,
                             "audio": audio.attrib.get("url", "") if audio is not None else "",
                             "transcripts": transcripts})
    episodes.sort(key=lambda x: x["published"], reverse=True)
    return episodes[:show["count"]]


def transcript_from_link(link):
    response = requests.get(link["url"], timeout=30)
    response.raise_for_status()
    if len(response.content) > 5_000_000:
        raise RuntimeError("Transcript is too large")
    kind = link["type"].lower()
    if "json" in kind or link["url"].split("?", 1)[0].endswith(".json"):
        value = response.json()
        if isinstance(value, dict):
            value = value.get("segments", value.get("transcript", value.get("text", "")))
        if isinstance(value, list):
            text = " ".join(str(part.get("text", part.get("body", ""))) if isinstance(part, dict)
                            else str(part) for part in value)
        else:
            text = str(value)
    else:
        lines = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line or re.fullmatch(r"\d+", line) or "-->" in line or line.upper() == "WEBVTT":
                continue
            lines.append(line)
        text = " ".join(lines)
    text = clean_text(text)
    if len(text) < 300:
        raise RuntimeError("Transcript has too little speech")
    return text


def audio_transcript(url):
    global whisper_model
    if not url:
        raise RuntimeError("This episode has no audio address")
    with tempfile.NamedTemporaryFile(prefix="leave-podcast-", suffix=".audio", delete=False) as target:
        path = Path(target.name)
        try:
            with requests.get(url, stream=True, timeout=45,
                              headers={"User-Agent": "Mozilla/5.0"}) as response:
                response.raise_for_status()
                size = 0
                for chunk in response.iter_content(128 * 1024):
                    size += len(chunk)
                    if size > 250_000_000:
                        raise RuntimeError("Episode audio is over 250 MB")
                    target.write(chunk)
            if whisper_model is None:
                from faster_whisper import WhisperModel
                whisper_model = WhisperModel(os.environ.get("PODCAST_SPEECH_MODEL", "base"),
                                             device="cpu", compute_type="int8", cpu_threads=4)
            segments, _ = whisper_model.transcribe(str(path), language="en", beam_size=1,
                                                    vad_filter=True, condition_on_previous_text=False)
            text = clean_text(" ".join(segment.text for segment in segments))
            if len(text) < 300:
                raise RuntimeError("Too little speech was recognised")
            return text
        finally:
            path.unlink(missing_ok=True)


def speech_for(episode):
    cache = DATA / "transcripts" / (hashlib.sha256(episode["guid"].encode()).hexdigest() + ".txt")
    if cache.exists() and len(cache.read_text(encoding="utf-8")) >= 300:
        return cache.read_text(encoding="utf-8")
    errors = []
    for link in episode["transcripts"]:
        try:
            text = transcript_from_link(link)
            break
        except Exception as exc:
            errors.append(str(exc))
    else:
        text = audio_transcript(episode["audio"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def model_answer(system, prompt, limit=700):
    if not MODEL_URL or not MODEL_NAME:
        raise RuntimeError("No text model is connected")
    headers = {}
    key = os.environ.get("PODCAST_MODEL_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": MODEL_NAME, "messages": [{"role": "system", "content": system},
              {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": limit}
    if os.environ.get("PODCAST_DISABLE_REASONING") == "1":
        payload["reasoning_effort"] = "none"
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    response = requests.post(MODEL_URL, json=payload, headers=headers, timeout=180)
    response.raise_for_status()
    return (response.json()["choices"][0]["message"].get("content") or "").strip()


def episode_notes(episode, speech):
    speech = re.sub(r"\b(one|two|three|four|five) and (one|two|three|four|five) who\b",
                    "[unclear number] who", speech, flags=re.IGNORECASE)
    parts = [speech[i:i + 18000] for i in range(0, len(speech), 18000)]
    system = ("Extract concrete claims from podcast speech. Say who said them if clear. "
              "Keep numbers, reasons, disagreements and caveats. If speech is unclear, "
              "do not repair a number by guessing. Do not invent facts. "
              "Give at most five short points. No introduction.")
    notes = []
    for index, part in enumerate(parts, 1):
        prompt = ("SHOW: " + episode["show"] + "\nEPISODE: " + episode["title"] +
                  "\nPART: " + str(index) + "/" + str(len(parts)) + "\nSPEECH:\n" + part)
        digest = hashlib.sha256((MODEL_URL + MODEL_NAME + system + prompt).encode()).hexdigest()
        path = DATA / "notes" / (digest + ".txt")
        if path.exists() and path.stat().st_size > 30:
            note = path.read_text(encoding="utf-8")
        else:
            note = model_answer(system, prompt)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix="note-", delete=False) as temporary:
                temporary.write(note)
                temporary_path = Path(temporary.name)
            temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
        notes.append(episode["show"] + " — " + episode["title"] + ":\n" + note)
    return notes


def extractive_points(episode, speech, limit=3):
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", speech)
    candidates = []
    for index, sentence in enumerate(sentences):
        words = sentence.split()
        if not 9 <= len(words) <= 32 or sentence.endswith("?") or not sentence.endswith((".", "!")):
            continue
        lower = sentence.casefold()
        if any(x in lower for x in ("subscribe", "our sponsor", "promo code", "welcome back")):
            continue
        if re.search(r"\b(?:one|two|three) and (?:one|two|three|four|five)\b", lower):
            continue
        score = sum(x in lower for x in ("because", "means", "however", "but ", "risk", "cost",
                                      "better", "worse", "important", "changed", "instead")) * 2
        score += 3 if re.search(r"\d", sentence) else 0
        candidates.append((index, score, sentence.strip()))
    result = []
    def take(options):
        for _, _, sentence in sorted(options, key=lambda x: x[1], reverse=True):
            words = set(re.findall(r"[a-z]{4,}", sentence.casefold()))
            if any(len(words & old) / max(1, len(words | old)) > .30 for _, old in result):
                continue
            result.append((sentence, words))
            return
    for part in range(min(limit, 3)):
        take([item for item in candidates if part * len(sentences) // 3 <= item[0]
              < (part + 1) * len(sentences) // 3])
    while len(result) < limit:
        before = len(result)
        take(candidates)
        if len(result) == before:
            break
    return [episode["show"] + ': “' + sentence + '”' for sentence, _ in result]


def make_report_lines(episodes, angle="", count=3):
    if MODEL_URL and MODEL_NAME:
        notes = []
        for episode, speech in episodes:
            notes.extend(episode_notes(episode, speech))
        final_number = count + 1
        system = ("Write EXACTLY " + str(final_number) + " numbered lines, each at most 35 words. "
                  "Lines 1 to " + str(count) + " each give one specific claim and why it matters. "
                  "Include each readable podcast at least once when there are enough lines, in the order "
                  "listed. If there are more podcasts than lines, pick the strongest distinct claims. "
                  "Never invent a missing show. Attribute each claim. Line " + str(final_number) +
                  " starts 'Cynic's view:' and applies the owner's angle at the end: "
                  "explain the practical importance, who might benefit, or what evidence is missing. "
                  "A possible hidden motive must say 'might', 'could', or be a question, never a fact. "
                  "Preserve numbers exactly; a percentage below 50 is not a majority. Never guess what "
                  "unclear speech means. Do not invent facts. Use plain words and no filler.")
        prompt = "OWNER'S ANGLE: " + (angle or "No special angle") + "\nNOTES:\n" + "\n\n".join(notes)
        answer = model_answer(system, prompt, limit=max(550, min(2400, final_number * 110)))
        numbered = {}
        for line in answer.splitlines():
            match = re.match(r"^\s*(\d+)[.)]\s*(.+)", line)
            if match:
                numbered[int(match.group(1))] = match.group(2).strip()
        if all(index in numbered for index in range(1, final_number + 1)):
            cynic = re.sub(r"^Cynic(?:'s|’s)? view:\s*", "", numbered[final_number], flags=re.IGNORECASE)
            return [numbered[index] for index in range(1, final_number)], cynic
    buckets = [extractive_points(episode, speech, limit=count) for episode, speech in episodes]
    points = []
    while len(points) < count and any(buckets):
        for bucket in buckets:
            if bucket and len(points) < count:
                points.append(bucket.pop(0))
    return points or ["No clear point was found in the available speech."], ""


def episode_id(episode):
    return hashlib.sha256(episode["guid"].encode()).hexdigest()


def sent_episode_ids():
    ids = set()
    if SENT_EPISODES.exists():
        ids.update(json.loads(SENT_EPISODES.read_text(encoding="utf-8")).get("ids", []))
    if LAST.exists():
        previous = json.loads(LAST.read_text(encoding="utf-8"))
        if previous.get("sent"):
            ids.update(previous.get("used_ids", []))
    return ids


def retire_sent_episodes(result):
    save_json(SENT_EPISODES, {"ids": sorted(sent_episode_ids() | set(result.get("used_ids", [])))})
    result["episodes"] = []


def report(config, progress=lambda stage: None):
    selected, readable, problems = [], [], []
    already_sent = sent_episode_ids()
    feeds_read = 0
    for show in config["shows"]:
        progress("Finding episodes from " + show["name"])
        try:
            episodes = read_feed(show)
            feeds_read += 1
        except Exception as exc:
            problems.append(show["name"] + ": " + str(exc)[:120])
            continue
        for episode in episodes:
            if episode_id(episode) in already_sent:
                continue
            selected.append({key: episode[key] for key in ("show", "title", "published", "url")})
            progress("Reading " + episode["title"][:50])
            try:
                readable.append((episode, speech_for(episode)))
            except Exception as exc:
                problems.append(episode["title"] + ": " + str(exc)[:120])
    if not selected:
        if not feeds_read and problems:
            raise RuntimeError("No podcast episode lists could be read. " + "; ".join(problems[:2]))
        return {"at": datetime.now(timezone.utc).isoformat(), "episodes": [], "usable": 0,
                "used_ids": [], "lines": [], "cynic": "", "no_new": True,
                "message": "No new episodes since the last report.", "problems": problems}
    if not readable:
        raise RuntimeError("No episode speech could be read. " + "; ".join(problems[:2]))
    count = config.get("lines", 3)
    progress("Writing the report")
    lines, cynic = make_report_lines(readable, config.get("angle", ""), count)
    day = datetime.now(ZoneInfo(config["timezone"])).strftime("%d %B %Y")
    return {"at": datetime.now(timezone.utc).isoformat(), "episodes": selected, "usable": len(readable),
            "used_ids": sorted(episode_id(episode) for episode, _ in readable),
            "lines": lines, "cynic": cynic, "no_new": False,
            "message": "Leave the Podcasts · " + day + "\n" + "\n".join(lines) +
                       ("\nCynic's view: " + cynic if cynic else ""),
            "problems": problems}


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TEST_BOT_TOKEN", "")
    if not token or not TELEGRAM_CHAT:
        raise RuntimeError("Set your Telegram bot and chat number")
    response = requests.post("https://api.telegram.org/bot" + token + "/sendMessage",
                             json={"chat_id": TELEGRAM_CHAT, "text": message}, timeout=25)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram refused the report")


def recent_preview(config):
    if not LAST.exists():
        return None
    saved = json.loads(LAST.read_text(encoding="utf-8"))
    if (saved.get("kind") != "preview" or saved.get("sent") or saved.get("no_new")
            or saved.get("settings") != config or not saved.get("used_ids")
            or set(saved["used_ids"]) & sent_episode_ids()):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved["at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None
    return saved if 0 <= age < 7200 else None


def run(kind, automatic=False):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another report is already running") from None
        config = settings()
        result = recent_preview(config) if kind == "send" and not automatic else None
        if result is None:
            result = report(config, lambda stage: job.update(stage=stage))
        result["kind"] = kind
        result["settings"] = config
        result["sent"] = False
        if kind == "send":
            if not result.get("no_new"):
                send_telegram(result["message"])
                retire_sent_episodes(result)
                result["sent"] = True
            if result["sent"] or automatic:
                save_json(DELIVERY, {"date": datetime.now(ZoneInfo(config["timezone"])).date().isoformat()})
        save_json(LAST, result)
        return result


def start(kind):
    if not job_lock.acquire(blocking=False):
        raise RuntimeError("A report is already running")
    job.update(running=True, stage="Starting", error=None, kind=kind)

    def work():
        try:
            result = run(kind)
            stage = "Sent to Telegram" if result["sent"] else (
                "No new episodes to send" if result.get("no_new") else "Preview ready")
            job.update(stage=stage, running=False)
        except Exception as exc:
            job.update(stage="Could not finish", running=False, error=str(exc)[:300])
        finally:
            job_lock.release()
    threading.Thread(target=work, daemon=True).start()


@app.get("/")
def home():
    if not authorised():
        return render_template("open.html", base=BASE), 403
    return render_template("index.html", base=BASE)


@app.get("/about")
def about():
    return render_template("public.html", base=BASE, source_available=SOURCE_FILE.exists(),
                           repo_url=os.environ.get("PODCAST_REPO_URL", ""))


@app.get("/hero.png")
def hero():
    return send_file(ROOT / "assets" / "github-hero.png", mimetype="image/png")


@app.get("/source.zip")
def source_download():
    if not SOURCE_FILE.exists():
        abort(404)
    return send_file(SOURCE_FILE, as_attachment=True, download_name="leave-podcasts-source.zip")


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/open")
def open_key():
    if not secrets.compare_digest(request.args.get("key", ""), opening_key()):
        abort(403)
    response = make_response(redirect(BASE + "/"))
    response.set_cookie("podcast_access", opening_key(), max_age=365 * 86400,
                        secure=bool(BASE) or request.is_secure, httponly=True, samesite="Lax")
    return response


@app.post("/api/open")
def open_from_private_link():
    key = str((request.get_json(silent=True) or {}).get("key", ""))
    if not key or not secrets.compare_digest(key, opening_key()):
        abort(403)
    response = jsonify(opened=True)
    response.set_cookie("podcast_access", key, max_age=365 * 86400,
                        secure=bool(BASE) or request.is_secure, httponly=True, samesite="Strict",
                        path=BASE or "/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/status")
def status():
    need_access()
    last = json.loads(LAST.read_text(encoding="utf-8")) if LAST.exists() else None
    return jsonify(settings=settings(), last=last, job=job)


@app.get("/api/search")
def search():
    need_access()
    query = request.args.get("q", "").strip()[:100]
    if len(query) < 2:
        return jsonify(results=[])
    try:
        return jsonify(results=search_shows(query))
    except Exception as exc:
        return jsonify(error=str(exc)[:200]), 502


@app.post("/api/settings")
def save_settings():
    need_access()
    if request.headers.get("X-Podcast-Action") != "yes":
        abort(403)
    try:
        value = validate(request.get_json(force=True))
        save_json(SETTINGS, value)
        return jsonify(settings=value)
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/run/<kind>")
def run_web(kind):
    need_access()
    if request.headers.get("X-Podcast-Action") != "yes":
        abort(403)
    if kind not in ("preview", "send"):
        abort(404)
    try:
        start(kind)
        return jsonify(started=True), 202
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409


def daily_due(config):
    if not config["enabled"] or not config["shows"]:
        return False
    now = datetime.now(ZoneInfo(config["timezone"]))
    if now.strftime("%H:%M") < config["time"]:
        return False
    if DELIVERY.exists():
        return json.loads(DELIVERY.read_text(encoding="utf-8")).get("date") != now.date().isoformat()
    return True


def daily_loop():
    while True:
        try:
            if daily_due(settings()):
                run("send", automatic=True)
        except Exception as exc:
            print("Morning podcast report could not be sent: " + str(exc)[:200], flush=True)
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "preview", "send", "daily"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19134)
    args = parser.parse_args()
    if args.mode == "serve":
        if os.environ.get("PODCAST_EMBEDDED_DAILY") == "1":
            threading.Thread(target=daily_loop, daemon=True).start()
        app.run(host=args.host, port=args.port)
    elif args.mode != "daily" or daily_due(settings()):
        result = run("send" if args.mode in ("send", "daily") else "preview",
                     automatic=args.mode == "daily")
        print(result["message"])


if __name__ == "__main__":
    main()
