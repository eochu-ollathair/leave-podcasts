#!/usr/bin/env python3
# READ FIRST: /root/app/leave-podcasts/PROJECT_NOTES.md
"""Read podcast speech and send separate short reports for each show."""

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
from datetime import datetime, timedelta, timezone
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
SENT_MESSAGES = DATA / "sent_messages.json"
CHARTS = DATA / "charts"
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
    return {"shows": [], "blocked_shows": [], "lines": 2, "trending_count": 3,
            "popular_points": 1, "angle": "", "enabled": False,
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
    blocked_shows = []
    raw_blocked = value.get("blocked_shows", [])
    if not isinstance(raw_blocked, list) or len(raw_blocked) > 100:
        raise ValueError("Choose up to 100 podcasts to ignore")
    for item in raw_blocked:
        if not isinstance(item, dict):
            raise ValueError("A podcast to ignore needs a name")
        name = str(item.get("name", "")).strip()[:100]
        feed = str(item.get("feed", "")).strip()[:1200]
        if name and (not feed or urlparse(feed).scheme in ("http", "https")):
            blocked_shows.append({"name": name, "feed": feed})
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
        raise ValueError("Choose a whole number of points per podcast") from None
    if not 1 <= lines <= 20:
        raise ValueError("Choose 1 to 20 points per podcast")
    try:
        trending_count = int(value.get("trending_count", 3))
    except (TypeError, ValueError):
        raise ValueError("Choose a whole number of popular podcasts") from None
    if not 0 <= trending_count <= 5:
        raise ValueError("Choose 0 to 5 popular podcasts")
    try:
        popular_points = int(value.get("popular_points", 1))
    except (TypeError, ValueError):
        raise ValueError("Choose a whole number of points per popular episode") from None
    if not 1 <= popular_points <= 5:
        raise ValueError("Choose 1 to 5 points per popular episode")
    return {"shows": shows, "blocked_shows": blocked_shows,
            "lines": lines, "trending_count": trending_count, "popular_points": popular_points,
            "angle": str(value.get("angle", "")).strip()[:400],
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


def chart_path(day):
    return CHARTS / (day.isoformat() + ".json")


def capture_chart(now=None):
    now = now or datetime.now(ZoneInfo(settings()["timezone"]))
    path = chart_path(now.date())
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(previous["checked_at"])
        if checked.hour >= 23 or now.hour < 23:
            return previous
    response = requests.get("https://podcasts.apple.com/ie/charts/episodes", timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    if len(response.content) > 3_000_000 or "Trending Episodes" not in response.text:
        raise RuntimeError("Ireland's popular podcast list could not be read")
    links = re.findall(r'<a[^>]+data-testid="click-action"[^>]+href="(https://podcasts\.apple\.com/ie/podcast/[^\"]+\?i=\d+[^\"]*)"',
                       response.text)
    ranked, seen = [], set()
    for link in links:
        match = re.search(r"/id(\d+)\?i=(\d+)", html.unescape(link))
        if match and match.group(2) not in seen:
            ranked.append({"rank": len(ranked) + 1, "show_id": match.group(1),
                           "episode_id": match.group(2), "url": html.unescape(link)})
            seen.add(match.group(2))
    if len(ranked) < 10:
        raise RuntimeError("Ireland's popular podcast list did not contain enough episodes")
    snapshot = {"day": now.date().isoformat(), "checked_at": now.isoformat(),
                "source": "Apple Podcasts Ireland trending episodes", "ranked": ranked}
    save_json(path, snapshot)
    return snapshot


def previous_chart(config):
    today = datetime.now(ZoneInfo(config["timezone"]))
    path = chart_path(today.date() - timedelta(days=1))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def trending_episode(entry):
    response = requests.get("https://itunes.apple.com/lookup",
                            params={"id": entry["show_id"], "entity": "podcastEpisode",
                                    "country": "ie", "limit": 200}, timeout=25)
    response.raise_for_status()
    results = response.json().get("results", [])
    show = next((x for x in results if x.get("kind") == "podcast" and x.get("feedUrl")), None)
    listed = next((x for x in results if str(x.get("trackId")) == entry["episode_id"]), None)
    if not show or not listed:
        return None
    name = show.get("collectionName", "").strip()
    title = listed.get("trackName", "").strip()
    if not name or not title:
        return None
    episodes = read_feed({"name": name, "feed": show["feedUrl"], "count": 100})
    normal = lambda value: re.sub(r"[^a-z0-9]+", "", value.casefold())
    matching = next((x for x in episodes if normal(x["title"]) == normal(title)), None)
    if matching:
        matching.update(chart_rank=entry["rank"], chart_url=entry["url"])
    return matching


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
            episode_url = child_text(item, "link") or show["feed"]
            if urlparse(episode_url).scheme not in ("http", "https"):
                episode_url = show["feed"]
            episodes.append({"show": show["name"], "title": title, "published": published,
                             "url": episode_url, "feed": show["feed"],
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
    system = ("Extract concrete claims from podcast speech. Say who said them only if clear. "
              "A host and guest may answer each other; never assign one person's words to the "
              "other. If unsure, say 'a speaker'. "
              "Keep the numbers as spoken, with their original units. Never calculate a new "
              "price, rate or percentage from numbers in the speech. Keep reasons, disagreements "
              "and caveats, including when a number comes from a speaker or company's own test. "
              "If speech is unclear, do not repair a number by guessing. Ignore adverts and "
              "introductions. Give at most six short points from different subjects where possible. "
              "Do not invent facts. No introduction.")
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
        notes.append("Part " + str(index) + " of " + str(len(parts)) + " — " +
                     episode["show"] + " — " + episode["title"] + ":\n" + note)
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


def plain_report_words(value):
    replacements = {"AI": "artificial intelligence", "US": "United States",
                    "UK": "United Kingdom", "EV": "electric car", "EVs": "electric cars"}
    for short, full in replacements.items():
        value = re.sub(r"\b" + re.escape(short) + r"\b", full, value)
    return value.strip()


def sceptical_view(points, angle):
    money_points = [point for point in points if re.search(
        r"\b(?:compensat\w*|paid|payment|profit\w*|received money)\b",
        point["claim"], flags=re.IGNORECASE)]
    focus_points = money_points or points
    system = ("Write one short sceptical view beneath this podcast only. Choose ONE named subject "
              "from its points. If a point explicitly names who received money or power, state "
              "that visible benefit and the record that would confirm it. Otherwise name the "
              "specific study, account or repeat test needed to check one claim. "
              "Do not guess why any law or action happened. Do not invent beneficiaries, people, "
              "products or transactions. Do not say one result settles a broad question. "
              "Do not discuss other podcasts or ask a question. Use ordinary words, no shortened "
              "terms or vague phrases like 'more evidence'. At most 14 words.")
    answer = model_answer(system, "OWNER'S ANGLE: " + (angle or "Be sceptical") +
                          "\nTHIS PODCAST'S POINTS:\n" + json.dumps(focus_points), limit=180)
    view = plain_report_words(re.sub(r"^\s*(?:Cynic(?:'s|’s)? view:\s*)?", "", answer,
                                     flags=re.IGNORECASE).splitlines()[0] if answer else "")
    if not view:
        raise RuntimeError("Could not write the sceptical view")
    if len(view.split()) > 18:
        shorter = model_answer("Shorten this to one sentence of at most 14 words. Keep the "
                               "specific subject and exact check or named recipient. Do not add "
                               "a motive or new fact.", view, limit=100)
        view = plain_report_words(shorter.splitlines()[0] if shorter else "")
    if len(view.split()) > 18:
        raise RuntimeError("The sceptical view was too long")
    return view


def read_report_points(answer, count, show):
    try:
        parsed = json.loads(answer[answer.index("{"):answer.rindex("}") + 1])
        raw_points = parsed["points"]
        if not isinstance(raw_points, list) or len(raw_points) < count:
            raise ValueError("wrong number of points")
        raw_points = raw_points[:count]
        points = [{key: plain_report_words(str(item[key])) for key in
                   ("subject", "claim", "why")} for item in raw_points]
        if any(not all(point.values()) for point in points):
            raise ValueError("empty point")
        return points
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Could not write the requested points for " + show) from exc


def numbers_in_text(value):
    return {match.replace(",", "") for match in re.findall(
        r"(?<![A-Za-z])\d+(?:[,.]\d+)*", value)}


def check_spoken_numbers(points, speech, show):
    spoken = numbers_in_text(speech)
    for index, point in enumerate(points):
        missing = numbers_in_text(point["claim"] + " " + point["why"]) - spoken
        if not missing:
            continue
        keywords = [word.lower() for word in re.findall(r"[A-Za-z]{5,}", point["subject"])]
        locations = [speech.lower().find(word) for word in keywords]
        location = next((place for place in locations if place >= 0), 0)
        excerpt = speech[max(0, location - 1500):location + 7000]
        system = ("Rewrite this ONE point using the original speech. The listed numbers were "
                  "not found in the speech and must be removed or replaced by the exact spoken "
                  "amounts. Keep the claim within 16 words and the reason within 6 words. "
                  "Keep the same subject and practical reason. Do not calculate a new "
                  "price or percentage. Return only JSON in this form: "
                  '{"points":[{"subject":"...","claim":"...","why":"..."}]}')
        answer = model_answer(system, "UNSUPPORTED NUMBERS: " + ", ".join(sorted(missing)) +
                              "\nPOINT: " + json.dumps(point) + "\nSPEECH:\n" + excerpt, limit=350)
        points[index] = read_report_points(answer, 1, show)[0]
        if numbers_in_text(points[index]["claim"] + " " + points[index]["why"]) - spoken:
            raise RuntimeError("A number in " + show + " was not heard in the episode")
    return points


def title_speech(episode, speech):
    """Use the part of a popular episode that actually discusses its title."""
    title_words = {word.casefold() for word in re.findall(r"[A-Za-z]{5,}", episode["title"])
                   if word.casefold() not in {"episode", "extra", "about", "there", "their",
                                              "after", "before", "part", "today", "podcast"}}
    if len(title_words) < 2:
        return ""
    candidates = []
    for word in title_words:
        for match in list(re.finditer(r"\b" + re.escape(word) + r"\b", speech, re.I))[:12]:
            start = max(0, match.start() - 1300)
            end = min(len(speech), match.end() + 4000)
            excerpt = speech[start:end]
            present = sum(bool(re.search(r"\b" + re.escape(term) + r"\b", excerpt, re.I))
                          for term in title_words)
            candidates.append((present, start, excerpt))
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: (item[0], -item[1]))
    return best[2] if best[0] >= 2 else ""


def episode_report(episode, speech, angle, count):
    if MODEL_URL and MODEL_NAME:
        focus = title_speech(episode, speech) if episode.get("chart_rank") and count == 1 else ""
        notes = ["Speech about the title:\n" + focus] if focus else episode_notes(episode, speech)
        system = ("Read notes from every part of this one podcast episode. Write exactly " + str(count) +
                  " points about different subjects when the episode covers several subjects. "
                  "Do not just take the first subject. Each point has a short subject, one specific "
                  "claim heard in the episode, and a practical reason that claim matters. "
                  "Do not name a speaker or guest in the claim: speech text does not reliably "
                  "identify which voice said which words. Attribute an outside study or company "
                  "when the episode names that source. "
                  "For one popular episode point, cover the situation named in its title "
                  "when that situation appears in the supplied speech. Do not choose a side topic. "
                  "Prefer subjects named in the episode title when they are actually discussed. "
                  "Each subject is at most three words, each claim at most 16 words, each reason "
                  "at most 6 words. This is a two-minute morning report. Keep necessary caveats "
                  "and speaker names even when shortening. Use plain words; omit obscure test names. "
                  "Keep each reason tied to its own claim. Ignore adverts, introductions and show news. "
                  "If a result came from one study, a simulation, or a company's own test, say so "
                  "and name who reported the number. Do not turn their claim into a general fact. "
                  "Preserve numbers exactly. Spell out shortened terms. "
                  "Return only a JSON object with this form: "
                  '{"points":[{"subject":"...","claim":"...","why":"..."}]}')
        prompt = "EPISODE: " + episode["title"] + "\nNOTES:\n" + "\n\n".join(notes)
        answer = model_answer(system, prompt, limit=max(650, count * 175))
        try:
            points = read_report_points(answer, count, episode["show"])
        except RuntimeError:
            answer = model_answer("Rewrite this as valid JSON with exactly " + str(count) +
                                  " points, each with subject, claim and why. Preserve the "
                                  "original claims and caveats. Add no new facts or numbers.",
                                  answer, limit=max(350, count * 150))
            points = read_report_points(answer, count, episode["show"])
        if any(len(point["claim"].split()) > 18 or len(point["why"].split()) > 8
               for point in points):
            shorter = model_answer("Shorten this report without changing any claim, number, caveat "
                                   "or speaker. Keep the same separate subjects. Each claim at most "
                                   "16 words and each reason at most 6 words. Return only the same "
                                   'JSON form: {"points":[{"subject":"...","claim":"...","why":"..."}]}',
                                   json.dumps({"points": points}), limit=max(650, count * 175))
            points = read_report_points(shorter, count, episode["show"])
        if any(len(point["claim"].split()) > 18 or len(point["why"].split()) > 8
               for point in points):
            raise RuntimeError("The points for " + episode["show"] + " were too long")
        points = check_spoken_numbers(points, speech, episode["show"])
        if any(len(point["claim"].split()) > 25 or len(point["why"].split()) > 12
               for point in points):
            raise RuntimeError("The checked points for " + episode["show"] + " were too long")
        view = sceptical_view(points, angle)
    else:
        quotes = extractive_points(episode, speech, limit=count)
        points = [{"subject": "What was said", "claim": quote.removeprefix(
            episode["show"] + ": "), "why": ""} for quote in quotes]
        view = ""
    return {"show": episode["show"], "title": episode["title"],
            "published": episode["published"], "url": episode["url"],
            "feed": episode["feed"],
            "points": points, "view": view,
            "chart_rank": episode.get("chart_rank"), "chart_day": episode.get("chart_day")}


def report_message(sections, timezone_name):
    day = datetime.now(ZoneInfo(timezone_name)).strftime("%d %B %Y")
    blocks = ["Leave the Podcasts · " + day]
    for section in sections:
        heading = section["show"] + " — " + section["title"]
        if section.get("chart_rank"):
            heading = ("Ireland popular list " + section["chart_day"] +
                       ", number " + str(section["chart_rank"]) + ": " + heading)
        lines = [heading]
        for number, point in enumerate(section["points"], 1):
            line = str(number) + ". " + point["claim"]
            if point["why"]:
                line += " Why: " + point["why"]
            lines.append(line)
        if section["view"]:
            lines.append("Cynic: " + section["view"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
    chosen_ids = set()
    feeds_read = 0
    def ignored(show):
        return any((item.get("feed") and item["feed"] == show["feed"]) or
                   item["name"].casefold() == show["name"].casefold()
                   for item in config.get("blocked_shows", []))

    for show in config["shows"]:
        if ignored(show):
            continue
        progress("Finding episodes from " + show["name"])
        try:
            episodes = read_feed(show)
            feeds_read += 1
        except Exception as exc:
            problems.append(show["name"] + ": " + str(exc)[:120])
            continue
        for episode in episodes:
            if episode_id(episode) in already_sent or episode_id(episode) in chosen_ids:
                continue
            selected.append({key: episode[key] for key in ("show", "title", "published", "url")})
            progress("Reading " + episode["title"][:50])
            try:
                readable.append((episode, speech_for(episode)))
                chosen_ids.add(episode_id(episode))
            except Exception as exc:
                problems.append(episode["title"] + ": " + str(exc)[:120])
    wanted = config.get("trending_count", 3)
    if wanted:
        try:
            chart = previous_chart(config) or capture_chart()
        except Exception as exc:
            chart = None
            problems.append("Ireland's popular list: " + str(exc)[:120])
        if chart:
            progress("Finding podcasts from Ireland's popular list, " + chart["day"])
        found = 0
        for entry in (chart["ranked"][:20] if chart else []):
            if found >= wanted:
                break
            try:
                episode = trending_episode(entry)
                if not episode:
                    problems.append("Popular list number " + str(entry["rank"]) +
                                    ": the episode was not in its public show list")
                    continue
                if ignored({"name": episode["show"], "feed": episode["feed"]}) \
                        or episode_id(episode) in already_sent or episode_id(episode) in chosen_ids:
                    continue
                episode["chart_day"] = chart["day"]
                progress("Reading popular episode " + str(entry["rank"]))
                speech = speech_for(episode)
                selected.append({key: episode[key] for key in ("show", "title", "published", "url")})
                readable.append((episode, speech))
                chosen_ids.add(episode_id(episode))
                found += 1
            except Exception as exc:
                problems.append("Popular list number " + str(entry["rank"]) + ": " + str(exc)[:120])
    if not selected:
        if not feeds_read and problems:
            raise RuntimeError("No podcast episode lists could be read. " + "; ".join(problems[:2]))
        return {"at": datetime.now(timezone.utc).isoformat(), "episodes": [], "usable": 0,
                "used_ids": [], "sections": [], "no_new": True,
                "message": "No new episodes since the last report.", "problems": problems}
    if not readable:
        raise RuntimeError("No episode speech could be read. " + "; ".join(problems[:2]))
    count = config.get("lines", 3)
    progress("Writing the report")
    sections, used_ids, reported = [], [], []
    for episode, speech in readable:
        try:
            sections.append(episode_report(episode, speech, config.get("angle", ""),
                                           config.get("popular_points", 1)
                                           if episode.get("chart_rank") else count))
            used_ids.append(episode_id(episode))
            reported.append({key: episode[key] for key in ("show", "title", "published", "url")})
        except Exception as exc:
            problems.append(episode["title"] + ": " + str(exc)[:120])
    if not sections:
        raise RuntimeError("No episode report could be written. " + "; ".join(problems[:2]))
    return {"at": datetime.now(timezone.utc).isoformat(), "episodes": reported, "usable": len(sections),
            "used_ids": used_ids,
            "sections": sections, "no_new": False,
            "message": report_message(sections, config["timezone"]),
            "problems": problems}


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TEST_BOT_TOKEN", "")
    if not token or not TELEGRAM_CHAT:
        raise RuntimeError("Set your Telegram bot and chat number")
    pieces, current = [], ""
    for line in message.splitlines():
        if len(line) > 3900:
            raise RuntimeError("A report line is too long to send")
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > 3900:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    message_ids = []
    for piece in pieces:
        response = requests.post("https://api.telegram.org/bot" + token + "/sendMessage",
                                 json={"chat_id": TELEGRAM_CHAT, "text": piece}, timeout=25)
        response.raise_for_status()
        answer = response.json()
        if not answer.get("ok"):
            raise RuntimeError("Telegram refused the report")
        message_ids.append((answer.get("result") or {}).get("message_id"))
    return message_ids


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
                sent = []
                mapping = json.loads(SENT_MESSAGES.read_text(encoding="utf-8")) if SENT_MESSAGES.exists() else {}
                for section, used_id in zip(result["sections"], result["used_ids"]):
                    ids = send_telegram(report_message([section], config["timezone"]))
                    sent.extend(ids)
                    for message_id in ids:
                        mapping[str(message_id)] = {"kind": "podcast", "name": section["show"],
                                                    "feed": section["feed"]}
                    save_json(SENT_MESSAGES, dict(list(mapping.items())[-200:]))
                    save_json(SENT_EPISODES, {"ids": sorted(sent_episode_ids() | {used_id})})
                result["telegram_message_ids"] = sent
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
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / "settings.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
    if not config["enabled"] or (not config["shows"] and not config.get("trending_count", 3)):
        return False
    now = datetime.now(ZoneInfo(config["timezone"]))
    if now.strftime("%H:%M") < config["time"]:
        return False
    if DELIVERY.exists():
        return json.loads(DELIVERY.read_text(encoding="utf-8")).get("date") != now.date().isoformat()
    return True


def daily_loop():
    while True:
        config = settings()
        try:
            if config.get("trending_count", 3):
                capture_chart(datetime.now(ZoneInfo(config["timezone"])))
        except Exception as exc:
            print("Ireland's popular podcast list could not be saved: " + str(exc)[:200], flush=True)
        try:
            if daily_due(config):
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
        from telegram_replies import start_listener
        start_listener("podcast", ROOT)
        app.run(host=args.host, port=args.port)
    elif args.mode != "daily" or daily_due(settings()):
        result = run("send" if args.mode in ("send", "daily") else "preview",
                     automatic=args.mode == "daily")
        print(result["message"])


if __name__ == "__main__":
    main()
