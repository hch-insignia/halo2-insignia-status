"""Halo Classic Hub status board. --dry-run never contacts Discord."""
import argparse
import hashlib
import json
import logging
import math
import os
import re
import signal
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HOME = "https://insignia.live/"
HALO = "https://insignia.live/halo2"
UTC = timezone.utc
LOG = logging.getLogger("halo2-feed")
STOP = threading.Event()


class SourceError(ValueError):
    pass


def count(text):
    text = text.strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise SourceError("Unrecognized count")
    return int(text)


def halo_row(table):
    if table is None:
        raise SourceError("Missing stats table")
    for row in table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if cells and cells[0].find("a", href=re.compile(r"/games/4d530064/?$", re.I)):
            return cells
    raise SourceError("Halo 2 row not present")


def parse_online(soup):
    section = soup.select_one("#games")
    cells = halo_row(section.find("table") if section else None)
    if len(cells) != 5:
        raise SourceError("Active-games columns changed")
    return count(cells[2].get_text(" ", strip=True))


def parse_in_game(soup):
    section = soup.select_one("#games")
    cells = halo_row(section.find("table") if section else None)
    if len(cells) != 5:
        raise SourceError("Active-games columns changed")
    rows = cells[3].select("tr[title]")
    values = []
    for row in rows:
        title = BeautifulSoup(row["title"], "html.parser").get_text(" ", strip=True).lower()
        if not any(term in title for term in ("public matches", "private matches", "in-game")):
            raise SourceError("Unrecognized active-player category")
        parts = row.find_all("td", recursive=False)
        if len(parts) != 2:
            raise SourceError("Active-player layout changed")
        # Public matches may be shown as occupied / available. Count occupied only.
        values.append(count(parts[1].get_text(" ", strip=True).split("/")[0]))
    if not values:
        raise SourceError("Missing in-game count")
    return sum(values)


def parse_weekly(soup):
    heading = soup.find(re.compile("^h[1-6]$"), string=re.compile(r"Popular this week"))
    if heading is None:
        raise SourceError("Missing weekly section")
    cells = halo_row(heading.find_next("table"))
    if len(cells) != 3:
        raise SourceError("Weekly columns changed")
    return count(cells[2].get_text(" ", strip=True))


def parse_playlists(soup):
    cards = soup.select('details[name="playlist-accordion"]')
    if not cards:
        raise SourceError("Missing playlist cards")
    result = {}
    for card in cards:
        summary = card.find("summary", recursive=False)
        if summary is None:
            raise SourceError("Missing playlist heading")
        match = re.fullmatch(r"(.+?)\s+([\d,]+)\s+Players?", summary.get_text(" ", strip=True))
        if not match or match[1] in result:
            raise SourceError("Unrecognized or duplicate playlist")
        result[match[1]] = count(match[2])
    return result


def parse_grid(soup):
    root = soup.select_one("#best-times[data-grid]")
    if root is None:
        raise SourceError("Missing best-times grid")
    grid = json.loads(root["data-grid"])
    if not isinstance(grid, list) or len(grid) != 7:
        raise SourceError("Invalid weekly grid")
    for row in grid:
        if not isinstance(row, list) or len(row) != 24:
            raise SourceError("Invalid hourly grid")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in row):
            raise SourceError("Invalid activity score")
    return grid  # Website defines [Monday..Sunday][UTC hour].


def best_today(grid, now, zone):
    if grid is None:
        return "Unavailable"
    today = now.astimezone(zone).date()
    start = datetime.combine(today, time.min, zone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(UTC)
    slots = []
    slot = start
    while slot < end:
        slots.append((grid[slot.weekday()][slot.hour], slot))
        slot += timedelta(hours=1)
    score, peak = max(slots, key=lambda item: item[0])
    if score <= 0:
        return "Not enough recent activity"
    a, b = peak.astimezone(zone), (peak + timedelta(hours=1)).astimezone(zone)
    def clock(t):
        return t.strftime("%I:%M %p").lstrip("0").replace(":00", "")
    suffix = " (ends tomorrow)" if b.date() != today else ""
    if a.tzname() != b.tzname():
        window = f"{clock(a)} {a.tzname()}–{clock(b)} {b.tzname()}"
    else:
        window = f"{clock(a)}–{clock(b)} {a.tzname()}"
    return window + suffix + " (historical estimate)"


def safe_parse(label, function, soup):
    if soup is None:
        return None
    try:
        return function(soup)
    except (SourceError, ValueError, TypeError, KeyError, IndexError):
        LOG.warning("%s unavailable: source layout or data not recognized", label)
        return None


def fetch_soup(session, url):
    try:
        r = session.get(url, timeout=(10, 25))
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException:
        LOG.warning("Insignia page could not be retrieved: %s", url)
        return None


def snapshot(home, halo, now, zone):
    grid = safe_parse("Best time", parse_grid, halo)
    return {
        "online": safe_parse("Online", parse_online, home),
        "in_game": safe_parse("In-game", parse_in_game, home),
        "weekly": safe_parse("Weekly", parse_weekly, home),
        "playlists": safe_parse("Playlists", parse_playlists, halo),
        "best": best_today(grid, now, zone),
    }


def payload(data, now):
    def display(value):
        return "Unavailable" if value is None else f"{value:,}"
    lists = data["playlists"]
    if lists is None:
        active = "Playlist information temporarily unavailable."
    else:
        top_five = sorted(
            ((name, number) for name, number in lists.items() if number > 0),
            key=lambda x: (-x[1], x[0]),
        )[:5]
        active = "\n".join(
            f"- {name}: **{number:,}**"
            for name, number in top_five
        ) or "No active playlists reported right now."
    description = (
        f"**Players Online:** {display(data['online'])}\n"
        f"**Players in Game:** {display(data['in_game'])}\n\n"
        f"**[Active Playlists]({HALO}):**\n{active}\n\n"
        f"**Additional Stats:**\n"
        f"- Best Time to Play Today: {data['best']}\n"
        f"- Unique Players This Week: **{display(data['weekly'])}**\n\n"
        "*Halo 2 only • Weekly = past 7 days, counted by gamertag.*"
    )
    if len(description) > 4096:
        raise SourceError("Status exceeds Discord embed limit")
    partial = any(data[k] is None for k in ("online", "in_game", "weekly", "playlists")) or data["best"] == "Unavailable"
    return {"content": "", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": "H2 Insignia Status", "url": HOME,
        "description": description, "color": 0xE5A436 if partial else 0x57F287,
        "footer": {"text": "Checked • Some data unavailable" if partial else "Checked • Source: insignia.live"},
        "timestamp": now.isoformat(),
    }]}


def webhook_base(value):
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ("discord.com", "discordapp.com")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d+/[\w.-]+", parsed.path)):
        raise ValueError("Set DISCORD_WEBHOOK_URL to a Discord channel webhook URL")
    return value


def read_state(path):
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError()
        return state
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        # Do not create duplicate boards silently when saved state is corrupt.
        raise ValueError("State cannot be read; restore it or set a valid message ID") from None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


class DiscordError(Exception):
    def __init__(self, status, code=None):
        super().__init__(f"Discord HTTP {status}; code {code}")
        self.status, self.code = status, code


def discord_request(session, method, url, body):
    for attempt in range(3):
        try:
            r = session.request(method, url, json=body, timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            # Exceptions may contain the secret URL; never log their text.
            raise DiscordError("network error") from None
        try:
            result = r.json()
        except ValueError:
            result = {}
        if r.status_code == 429:
            delay = max(1.0, float(result.get("retry_after", 60)))
            if STOP.wait(delay):
                raise DiscordError(429)
            continue
        if not 200 <= r.status_code < 300:
            raise DiscordError(r.status_code, result.get("code"))
        return result
    raise DiscordError(429)


def update_board(session, base, body, state, path):
    message_id = state.get("message_id")
    if message_id:
        try:
            discord_request(session, "PATCH", f"{base}/messages/{message_id}", body)
            return
        except DiscordError as error:
            if error.code != 10008:  # Recreate only when Discord says Unknown Message.
                raise
    result = discord_request(session, "POST", base + "?wait=true", body)
    if not str(result.get("id", "")).isdigit():
        raise DiscordError("missing message ID")
    state["message_id"] = result["id"]
    save_state(path, state)
    LOG.info("Created status message. DISCORD_MESSAGE_ID=%s", state["message_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch once, print preview; no Discord calls")
    parser.add_argument("--fixtures", type=Path, help="Read home.html and halo2.html from a local directory (dry-run only)")
    args = parser.parse_args()
    if args.fixtures and not args.dry_run:
        parser.error("--fixtures requires --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    zone = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "America/New_York"))
    interval = max(60, int(os.getenv("POLL_SECONDS", "60")))
    source = requests.Session()
    source.headers["User-Agent"] = "HaloClassicHub-Status/2.0 (public status; 60s minimum polling)"
    if args.dry_run:
        if args.fixtures:
            home, halo = [BeautifulSoup((args.fixtures / f"{name}.html").read_text(), "html.parser") for name in ("home", "halo2")]
        else:
            home, halo = fetch_soup(source, HOME), fetch_soup(source, HALO)
        now = datetime.now(UTC)
        data = snapshot(home, halo, now, zone)
        print(json.dumps(payload(data, now), indent=2))
        return
    base = webhook_base(os.getenv("DISCORD_WEBHOOK_URL", ""))
    path = Path(os.getenv("STATE_FILE", "state.json"))
    state = read_state(path)
    identity = hashlib.sha256(base.encode()).hexdigest()
    if state.get("webhook_hash") != identity:
        state = {"webhook_hash": identity}
    if not state.get("message_id") and os.getenv("DISCORD_MESSAGE_ID"):
        message_id = os.environ["DISCORD_MESSAGE_ID"].strip()
        if not message_id.isdigit():
            raise ValueError("DISCORD_MESSAGE_ID must contain digits only")
        state["message_id"] = message_id
    # Validate state storage before the first external post.
    save_state(path, state)
    discord = requests.Session()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: STOP.set())
    LOG.info("Starting Halo 2 board; refresh every %ss; timezone %s", interval, zone)
    while not STOP.is_set():
        home, halo = fetch_soup(source, HOME), fetch_soup(source, HALO)
        now = datetime.now(UTC)
        data = snapshot(home, halo, now, zone)
        if STOP.is_set():
            break
        try:
            update_board(discord, base, payload(data, now), state, path)
            LOG.info("Status updated; Halo 2 online=%s in_game=%s weekly=%s", data["online"], data["in_game"], data["weekly"])
        except DiscordError as error:
            LOG.error("%s", error)
            if error.status in (401, 403) or error.code in (10015, 50027):
                LOG.error("Check webhook URL and permissions in Railway variables")
                raise SystemExit(1)
        STOP.wait(interval)


if __name__ == "__main__":
    main()
