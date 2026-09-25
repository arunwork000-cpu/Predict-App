from django.conf import settings

from .flashlive import FlashLiveProvider


def get_providers():
    """The providers that are configured (have an API key), in priority order.

    Add another provider here (e.g. football-data.org) as a fallback for a
    sport FlashLive doesn't cover well.
    """
    providers = []
    if settings.RAPIDAPI_KEY:
        providers.append(FlashLiveProvider())
    return providers


def provider_for(sport_name, providers=None):
    """The first configured provider that covers `sport_name`, or None."""
    for provider in providers if providers is not None else get_providers():
        if provider.supports(sport_name):
            return provider
    return None
