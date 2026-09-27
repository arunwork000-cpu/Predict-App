from django.utils import timezone

from .constants import SUPPORTED_SPORTS
from .models import Match


def user_points(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, "profile", None)
        return {"user_points": profile.points if profile else 0}
    return {"user_points": None}


def public_nav(request):
    """The fixed public sport menu, available to every template's navbar.

    Each entry also carries `has_open`: whether that sport currently has at
    least one published, open-for-predictions match -- the same conditions
    as Match.predictions_open -- so the navbar can show a "Predict now"
    indicator under it. For a logged-in user, matches they have already
    predicted don't count, so the indicator goes away once every open match
    in a sport has been predicted.
    """
    open_matches = Match.objects.filter(
        is_published=True,
        status=Match.Status.SCHEDULED,
        winner__isnull=True,
        is_draw=False,
        prediction_deadline__gt=timezone.now(),
        sport__name__in=SUPPORTED_SPORTS,
    )
    if request.user.is_authenticated:
        open_matches = open_matches.exclude(predictions__user=request.user)
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
