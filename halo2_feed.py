"""Halo Classic Hub status feed: new posts on displayed changes. --dry-run is read-only."""
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
    session_rows = 0
    for row in rows:
        title = BeautifulSoup(row["title"], "html.parser").get_text(" ", strip=True).lower()
        if title.startswith(("public matches", "private matches")):
            session_rows += 1
            continue
        if title != "players currently in-game":
            raise SourceError("Unrecognized active-player category")
        parts = row.find_all("td", recursive=False)
        if len(parts) != 2:
            raise SourceError("Active-player layout changed")
        values.append(count(parts[1].get_text(" ", strip=True)))
    if len(values) == 1:
        return values[0]
    # The current page omits the gamepad row at zero. Require the known
    # session-table structure so a missing/broken table does not become zero.
    if not values and session_rows:
        return 0
    raise SourceError("Missing or duplicate in-game counter")


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


def delete_old_messages(session, base, state, path):
    """Retry saved deletions without posting another status or deleting the latest."""
    pending = list(dict.fromkeys(str(v) for v in state.get("pending_delete", [])))
    if not pending:
        return
    state["pending_delete"] = pending
    for message_id in pending.copy():
        if message_id == str(state.get("message_id")) or not message_id.isdigit():
            state["pending_delete"].remove(message_id)
            save_state(path, state)
            continue
        try:
            discord_request(session, "DELETE", f"{base}/messages/{message_id}", None)
        except DiscordError as error:
            if error.code != 10008:  # Already deleted is success; other failures retry.
                LOG.warning("Old-status cleanup pending: %s", error)
                continue
        state["pending_delete"].remove(message_id)
        save_state(path, state)
        LOG.info("Removed previous status message_id=%s", message_id)


def cleanup_history(webhook_session, bot_session, base, state, path):
    """Scan one history page per poll; delete ONLY posts by this exact webhook.

    The bot only reads IDs. Deletion uses the webhook's own token, so it cannot
    delete member messages or posts by a different webhook. No content intent
    or Manage Messages permission is needed.
    """
    if bot_session is None or not state.get("message_id"):
        return
    if state.get("history_cleanup_complete"):
        if datetime.now(UTC).timestamp() < state.get("history_next_scan", 0):
            return
        # Repeat hourly to catch orphaned posts after ambiguous network replies.
        state.pop("history_before", None)
        state["history_cleanup_complete"] = False
    if not state.get("history_before"):
        state["history_before"] = str(state["message_id"])
        save_state(path, state)
    meta = discord_request(webhook_session, "GET", base, None)
    expected_id = urlsplit(base).path.split("/")[-2]
    if not isinstance(meta, dict) or str(meta.get("id")) != expected_id or not str(meta.get("channel_id", "")).isdigit():
        raise DiscordError("webhook metadata not recognized")
    channel_id = meta["channel_id"]
    # A history request can return an empty list without READ_MESSAGE_HISTORY.
    # Verify access to our known live message before accepting end-of-history.
    latest = discord_request(bot_session, "GET",
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{state['message_id']}", None)
    if not isinstance(latest, dict) or str(latest.get("id")) != str(state["message_id"]):
        raise DiscordError("bot cannot read latest status message")
    cursor = state["history_before"]
    if not str(cursor).isdigit():
        raise ValueError("Invalid saved history cursor")
    messages = discord_request(bot_session, "GET",
        f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100&before={cursor}", None)
    if not isinstance(messages, list):
        raise DiscordError("channel history not recognized")
    ids = []
    pending = list(state.get("pending_delete", []))
    for message in messages:
        message_id = str(message.get("id", ""))
        if not message_id.isdigit() or int(message_id) >= int(cursor):
            raise DiscordError("invalid history pagination")
        ids.append(message_id)
        if str(message.get("webhook_id")) == expected_id and message_id != str(state["message_id"]):
            pending.append(message_id)
    # Persist deletion targets before advancing the cursor. Failed deletes
    # remain queued through restarts, even when the history scan is complete.
    state["pending_delete"] = list(dict.fromkeys(pending))
    if ids:
        state["history_before"] = min(ids, key=int)
    if len(messages) < 100:
        state["history_cleanup_complete"] = True
        state["history_next_scan"] = datetime.now(UTC).timestamp() + 3600
    save_state(path, state)
    delete_old_messages(webhook_session, base, state, path)
    LOG.info("History scan: checked %s messages; pending deletes=%s; scan complete=%s",
             len(messages), len(state["pending_delete"]), state.get("history_cleanup_complete", False))


def update_board(session, base, body, state, path):
    # Ignore the check timestamp. Changes outside the displayed top five do not
    # trigger an identical-looking alert. Persist the last successful post so
    # an unchanged restart does not send another notification.
    signature = hashlib.sha256(json.dumps(
        [{k: v for k, v in embed.items() if k != "timestamp"}
         for embed in body.get("embeds", [])],
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    message_id = state.get("message_id")
    if message_id and state.get("last_post_signature") == signature:
        try:
            discord_request(session, "PATCH", f"{base}/messages/{message_id}", body)
            delete_old_messages(session, base, state, path)
            return False
        except DiscordError as error:
            if error.code != 10008:  # Recreate only when Discord says Unknown Message.
                raise
            message_id = None
    result = discord_request(session, "POST", base + "?wait=true", body)
    if not str(result.get("id", "")).isdigit():
        raise DiscordError("missing message ID")
    state["message_id"] = result["id"]
    state["last_post_signature"] = signature
    if message_id and str(message_id) != str(result["id"]):
        state.setdefault("pending_delete", []).append(str(message_id))
    save_state(path, state)
    LOG.info("Posted status update; message_id=%s", state["message_id"])
    delete_old_messages(session, base, state, path)
    return True


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
    source.headers["User-Agent"] = "HaloClassicHub-Status/2.3 (public status; 60s minimum polling)"
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
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    history_session = None
    if bot_token:
        history_session = requests.Session()
        history_session.headers["Authorization"] = "Bot " + bot_token
    else:
        LOG.info("Previous tracked posts will be removed; add DISCORD_BOT_TOKEN for older untracked history cleanup")
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: STOP.set())
    LOG.info("Starting Halo 2 feed; new posts on displayed changes; check every %ss; timezone %s", interval, zone)
    while not STOP.is_set():
        home, halo = fetch_soup(source, HOME), fetch_soup(source, HALO)
        now = datetime.now(UTC)
        data = snapshot(home, halo, now, zone)
        if STOP.is_set():
            break
        try:
            posted = update_board(discord, base, payload(data, now), state, path)
            LOG.info("%s; Halo 2 online=%s in_game=%s weekly=%s", "New status posted" if posted else "No displayed changes; checked time refreshed", data["online"], data["in_game"], data["weekly"])
            try:
                cleanup_history(discord, history_session, base, state, path)
            except DiscordError as error:
                LOG.warning("History cleanup will retry; check bot token and View Channel / Read Message History permissions: %s", error)
        except DiscordError as error:
            LOG.error("%s", error)
            if error.status in (401, 403) or error.code in (10015, 50027):
                LOG.error("Check webhook URL and permissions in Railway variables")
                raise SystemExit(1)
        STOP.wait(interval)


if __name__ == "__main__":
    main()
