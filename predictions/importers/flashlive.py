"""FlashLive Sports API (RapidAPI): Flashscore's data, including its team and
player names, so imported names match the Team rows entered by hand.

https://rapidapi.com/tipsters/api/flashlive-sports

Quota: the free RapidAPI plan allows only a small number of requests a month,
so fixtures are fetched only for the tournaments listed in
FLASHLIVE_TOURNAMENTS, and results only for matches awaiting one.
"""

import logging
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings

from .base import REQUEST_TIMEOUT, ExternalEvent, Provider, ProviderError, make_session

logger = logging.getLogger(__name__)

API_HOST = "flashlive-sports.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}/v1"
LOCALE = "en_INT"

# Our Sport.name -> FlashLive sport_id. Only these sports are imported, and
# each one costs API quota on every run. Hockey (field hockey) is left out
# on purpose: it is entered by hand. Add a sport with FLASHLIVE_SPORT_IDS
# (field hockey is 24); check the IDs with
# `python manage.py sync_external_matches --list-sports`.
DEFAULT_SPORT_IDS = {
    "Football": 1,
    "Tennis": 2,
    "Cricket": 13,
    "Badminton": 21,
}

# FlashLive's list endpoint only reaches 7 days either side of today.
MAX_INDENT_DAYS = 7

CALLED_OFF_STAGES = {"POSTPONED", "CANCELED", "CANCELLED", "ABANDONED"}


class FlashLiveProvider(Provider):
    name = "flashlive"

    def __init__(self, api_key=None, tournaments=None, sport_ids=None, session=None):
        self.api_key = api_key if api_key is not None else settings.RAPIDAPI_KEY
        self.tournaments = {
            str(t).strip().lower()
            for t in (
                tournaments
                if tournaments is not None
                else settings.FLASHLIVE_TOURNAMENTS
            )
            if str(t).strip()
        }
        self.sport_ids = {
            **DEFAULT_SPORT_IDS,
            **{
                name: int(value)
                for name, value in (
                    sport_ids
                    if sport_ids is not None
                    else settings.FLASHLIVE_SPORT_IDS
                ).items()
            },
        }
        self.session = session or make_session(
            {"x-rapidapi-key": self.api_key, "x-rapidapi-host": API_HOST}
        )
        # Requests made by this instance, reported by the command so usage
        # against the monthly quota can be followed in the logs.
        self.requests_made = 0

    # -- Provider interface ------------------------------------------------

    def supports(self, sport_name):
        return sport_name in self.sport_ids

    def fetch_fixtures(self, sport_name, days_ahead, new_day_only=False):
        if not self.tournaments:
            # Without a filter this would import every match in the world.
            return []
        last_day = min(days_ahead, MAX_INDENT_DAYS)
        # new_day_only: just the day entering the window (1 request). Earlier
        # days were imported by previous daily runs.
        days = [last_day] if new_day_only else range(0, last_day + 1)
        events = []
        for day in days:
            for group in self._list_events(sport_name, day):
                if not self._tournament_wanted(group):
                    continue
                for raw in group.get("EVENTS") or []:
                    event = self._to_event(sport_name, group, raw)
                    if event:
                        events.append(event)
        return events

    def fetch_results(self, sport_name, kickoffs):
        # One request per kickoff day covers every match that day, so only
        # the days pending matches were played on are fetched.
        today = datetime.now(dt_timezone.utc).date()
        days = sorted({
            (start.astimezone(dt_timezone.utc).date() - today).days
            for start in kickoffs.values()
        })
        found = {}
        for day in days:
            if not -MAX_INDENT_DAYS <= day <= 0:
                continue
            for group in self._list_events(sport_name, day):
                for raw in group.get("EVENTS") or []:
                    if str(raw.get("EVENT_ID")) in kickoffs:
                        event = self._to_event(sport_name, group, raw)
                        if event:
                            found[event.external_id] = event
        return found

    def list_tournaments(self, sport_name, day=0):
        """[(full name, match count), ...] for one day, e.g.
        ("England: Premier League", 10). 1 request."""
        return sorted(
            (group.get("NAME") or "", len(group.get("EVENTS") or []))
            for group in self._list_events(sport_name, day)
        )

    def list_sports(self):
        """[(id, name), ...] as FlashLive numbers its sports."""
        data = self._get("/sports/list", {})
        return [(item.get("ID"), item.get("NAME")) for item in data or []]

    # -- helpers -----------------------------------------------------------

    def _get(self, path, params):
        if not self.api_key:
            raise ProviderError("RAPIDAPI_KEY is not set.")
        self.requests_made += 1
        try:
            response = self.session.get(
                BASE_URL + path, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise ProviderError(f"FlashLive request failed: {exc}") from exc
        if response.status_code == 404:
            # FlashLive answers 404 for a day with no events.
            return []
        if response.status_code != 200:
            raise ProviderError(
                f"FlashLive {path} returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            return response.json().get("DATA") or []
        except ValueError as exc:
            raise ProviderError(f"FlashLive {path} returned invalid JSON") from exc

    def _list_events(self, sport_name, indent_days):
        return self._get(
            "/events/list",
            {
                "sport_id": self.sport_ids[sport_name],
                "indent_days": indent_days,
                "locale": LOCALE,
                "timezone": 0,
            },
        )

    def _tournament_wanted(self, group):
        # Matches an ID, the full name ("ENGLAND: Premier League", the precise
        # choice) or the short name ("Premier League", which may also match
        # a same-named league in another country).
        keys = (
            group.get("TOURNAMENT_TEMPLATE_ID"),
            group.get("TOURNAMENT_STAGE_ID"),
            group.get("TOURNAMENT_ID"),
            group.get("NAME"),
            group.get("SHORT_NAME"),
            _event_name(group),
        )
        return any(str(k).strip().lower() in self.tournaments for k in keys if k)

    def _to_event(self, sport_name, group, raw):
        event_id = raw.get("EVENT_ID")
        home, away = raw.get("HOME_NAME"), raw.get("AWAY_NAME")
        start = raw.get("START_TIME") or raw.get("START_UTIME")
        if not (event_id and home and away and start):
            logger.warning("Skipping incomplete FlashLive event: %r", event_id)
            return None

        stage = str(raw.get("STAGE") or "").upper()
        called_off = stage in CALLED_OFF_STAGES
        finished = str(raw.get("STAGE_TYPE") or "").upper() == "FINISHED"

        return ExternalEvent(
            source=self.name,
            external_id=str(event_id),
            sport=sport_name,
            event_name=_event_name(group),
            home=home.strip(),
            away=away.strip(),
            start_time=datetime.fromtimestamp(int(start), tz=dt_timezone.utc),
            result=(
                _result(sport_name, raw) if finished and not called_off else None
            ),
            called_off=called_off,
            home_image=_first(raw.get("HOME_IMAGES")),
            away_image=_first(raw.get("AWAY_IMAGES")),
        )


def _event_name(group):
    name = group.get("SHORT_NAME") or group.get("NAME") or ""
    # "ENGLAND: Premier League" -> "Premier League"
    return name.split(": ", 1)[-1].strip()[:150]


def _first(images):
    if isinstance(images, list) and images:
        return str(images[0])
    return ""


def _result(sport_name, raw):
    """'home', 'away', 'draw' or None for a finished event."""
    winner = str(raw.get("WINNER", "")).strip()
    if winner == "1":
        return "home"
    if winner == "2":
        return "away"
    # A cricket score ("245/6") says nothing reliable about the winner or a
    # draw, so leave it for the admin to enter by hand.
    if sport_name == "Cricket":
        return None
    try:
        home = int(raw.get("HOME_SCORE_CURRENT"))
        away = int(raw.get("AWAY_SCORE_CURRENT"))
    except (TypeError, ValueError):
        return None
    if home > away:
        return "home"
    if away > home:
        return "away"
    return "draw"
