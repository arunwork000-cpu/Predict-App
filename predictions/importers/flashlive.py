"""FlashLive Sports API (RapidAPI): Flashscore's data, including its team and
player names, so imported names match the Team rows entered by hand.

https://rapidapi.com/tipsters/api/flashlive-sports

Quota: the free RapidAPI plan allows only a small number of requests a month.
One request returns a whole day of one sport, so which matches are imported
(FLASHLIVE_TOURNAMENTS / FLASHLIVE_TEAMS / FLASHLIVE_EXCLUDE_TOURNAMENTS) does
not change the number of requests.
"""

import logging
from fnmatch import fnmatchcase
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

    def __init__(
        self,
        api_key=None,
        tournaments=None,
        sport_ids=None,
        session=None,
        teams=None,
        exclude_tournaments=None,
    ):
        self.api_key = api_key if api_key is not None else settings.RAPIDAPI_KEY
        # Tournament names or IDs; "*" wildcards allowed, e.g. "* ATP*".
        self.tournaments = _patterns(
            tournaments if tournaments is not None else settings.FLASHLIVE_TOURNAMENTS
        )
        self.exclude_tournaments = _patterns(
            exclude_tournaments
            if exclude_tournaments is not None
            else settings.FLASHLIVE_EXCLUDE_TOURNAMENTS
        )
        # "Sport:Team" entries: import any match of that team, whatever the
        # tournament (e.g. every "Cricket:India" international).
        self.teams = {}
        for entry in teams if teams is not None else settings.FLASHLIVE_TEAMS:
            sport, sep, team = str(entry).partition(":")
            if sep and team.strip():
                self.teams.setdefault(sport.strip().lower(), set()).add(
                    team.strip().lower()
                )
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
        teams = self.teams.get(sport_name.lower(), set())
        if not (self.tournaments or teams):
            # Without a filter this would import every match in the world.
            return []
        last_day = min(days_ahead, MAX_INDENT_DAYS)
        # new_day_only: just the day entering the window (1 request). Earlier
        # days were imported by previous daily runs.
        days = [last_day] if new_day_only else range(0, last_day + 1)
        events = []
        for day in days:
            for group in self._list_events(sport_name, day):
                keys = _tournament_keys(group)
                if _matches_any(keys, self.exclude_tournaments):
                    continue
                whole_tournament = _matches_any(keys, self.tournaments)
                for raw in group.get("EVENTS") or []:
                    if not whole_tournament and not _plays(raw, teams):
                        continue
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


def _patterns(values):
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _tournament_keys(group):
    """The names/IDs a tournament can be listed by: an ID, the full name
    ("England: Premier League", the precise choice) or the short name
    ("Premier League", which may also match another country's league)."""
    keys = (
        group.get("TOURNAMENT_TEMPLATE_ID"),
        group.get("TOURNAMENT_STAGE_ID"),
        group.get("TOURNAMENT_ID"),
        group.get("NAME"),
        group.get("SHORT_NAME"),
        _event_name(group),
    )
    return [str(k).strip().lower() for k in keys if k]


def _matches_any(keys, patterns):
    """True if any key equals a pattern, or fits one with "*" wildcards."""
    return any(fnmatchcase(key, pattern) for key in keys for pattern in patterns)


def _plays(raw, teams):
    """True if one of `teams` (lower-case names) plays in this event."""
    return bool(teams) and any(
        str(raw.get(side) or "").strip().lower() in teams
        for side in ("HOME_NAME", "AWAY_NAME")
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
