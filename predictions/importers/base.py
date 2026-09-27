"""Provider interface for importing fixtures and results from an external
sports data source.

A provider turns its source's data into ExternalEvent objects, so the sync
logic in predictions.services never depends on any one provider's format.
"""

from dataclasses import dataclass, field
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REQUEST_TIMEOUT = 15  # seconds


class ProviderError(Exception):
    """The external source could not be reached or returned bad data."""


@dataclass
class ExternalEvent:
    source: str
    external_id: str
    sport: str  # our Sport.name, e.g. "Football"
    event_name: str
    home: str
    away: str
    start_time: datetime  # timezone-aware
    # "home", "away", "draw", or None while there is no final result.
    result: str | None = None
    # True if the source reports the match postponed/cancelled/abandoned.
    called_off: bool = False
    home_image: str = ""
    away_image: str = ""
    extra: dict = field(default_factory=dict)


class Provider:
    """Base class. Subclasses set `name` and implement the fetch methods."""

    name = ""

    def supports(self, sport_name):
        raise NotImplementedError

    def fetch_fixtures(self, sport_name, days_ahead, new_day_only=False):
        """Upcoming events for `sport_name`, as a list of ExternalEvent.

        new_day_only: only the day `days_ahead` days from today, for daily
        runs whose earlier days were already imported."""
        raise NotImplementedError

    def fetch_results(self, sport_name, kickoffs):
        """{external_id: ExternalEvent} for as many of `kickoffs`
        ({external_id: start_time}) as the source knows about. Events
        without a final result have result=None."""
        raise NotImplementedError

    def fetch_image(self, url):
        """Raw bytes of an image URL (team logo/flag), or None on failure.

        Uses a plain request, not the provider's session, so API keys in the
        session headers are never sent to the image host."""
        if not url.startswith("https://"):
            return None
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException:
            return None
        return response.content or None


def make_session(headers=None):
    """A requests session that retries transient failures (5xx, 429)."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    if headers:
        session.headers.update(headers)
    return session
