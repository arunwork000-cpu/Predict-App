from django.db import transaction
from django.db.models import F

from .models import Match, Profile

POINTS_CORRECT = 10
POINTS_WRONG = -5


def score_match(match_id):
    """Award points for a match exactly once. Returns True if scoring ran."""
    with transaction.atomic():
        match = Match.objects.select_for_update().get(pk=match_id)
        if match.is_scored:
            return False
        winning_side = match.winning_side()
        if winning_side is None:
            return False

        for prediction in match.predictions.select_related("user"):
            delta = (
                POINTS_CORRECT if prediction.choice == winning_side else POINTS_WRONG
            )
            profile, _ = Profile.objects.get_or_create(user=prediction.user)
            Profile.objects.filter(pk=profile.pk).update(points=F("points") + delta)

        match.is_scored = True
        match.save(update_fields=["is_scored"])
        return True
