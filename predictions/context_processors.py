from django.conf import settings

from . import google_auth, services
from .constants import SUPPORTED_SPORTS


def user_points(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, "profile", None)
        return {"user_points": profile.points if profile else 0}
    return {"user_points": None}


def google_login(request):
    """Whether to show the "Continue with Google" button."""
    return {"google_login_enabled": google_auth.is_enabled()}


def public_nav(request):
    """The fixed public sport menu, available to every template's navbar.

    Each entry also carries `has_open`: whether that sport currently has at
    least one published, open-for-predictions match -- the same conditions
    as Match.predictions_open -- so the navbar can show a "Predict now"
    indicator under it. For a logged-in user, matches they have already
    predicted don't count, so the indicator goes away once every open match
    in a sport has been predicted.
    """
    open_matches = services.open_matches(
        request.user if request.user.is_authenticated else None
    )
    open_sport_names = set(open_matches.values_list("sport__name", flat=True))
    return {
        "nav_sports": [
            {
                "slug": name.lower(),
                "name": name,
                "has_open": name in open_sport_names,
            }
            for name in SUPPORTED_SPORTS
        ]
    }


def app_alerts(request):
    """Match alerts (see predictions.push): the public key the browser
    subscribes with, and the number shown on the installed app's icon --
    open matches the user hasn't predicted yet."""
    context = {"vapid_public_key": settings.VAPID_PUBLIC_KEY}
    if request.user.is_authenticated:
        context["open_prediction_count"] = services.open_matches(request.user).count()
    return context
