from django.db import transaction
from django.db.models import F

from .models import Match, Profile

POINTS_CORRECT = 10
POINTS_WRONG = -5


def score_match(match_id):
    """Award or reconcile points for a match. Returns True if anything changed.

    Each prediction's award is stored on ``Prediction.points_awarded``. Scoring
    only applies the *delta* between the new award and the value already stored,
    so:

    * the first run awards +10 / -5 and records it on every prediction;
    * running again with the same winner is a no-op;
    * changing the winner and running again reconciles both the stored award
      and ``Profile.points`` without double counting;
    * clearing the winner of a scored match unwinds it: every awarded amount is
      subtracted back out, ``points_awarded`` returns to ``None`` and
      ``is_scored`` returns to ``False``.

    Runs inside a transaction with ``select_for_update`` so concurrent scoring
    of the same match is serialised.
    """
    with transaction.atomic():
        match = Match.objects.select_for_update().get(pk=match_id)
        winning_side = match.winning_side()

        changed = False

        if winning_side is None:
            # No winner. Unwind any points that were previously awarded.
            for prediction in match.predictions.filter(
                points_awarded__isnull=False
            ).select_related("user"):
                profile, _ = Profile.objects.get_or_create(user=prediction.user)
                Profile.objects.filter(pk=profile.pk).update(
                    points=F("points") - prediction.points_awarded
                )
                prediction.points_awarded = None
                prediction.save(update_fields=["points_awarded"])
                changed = True

            if match.is_scored:
                match.is_scored = False
                match.save(update_fields=["is_scored"])
                changed = True

            return changed

        for prediction in match.predictions.select_related("user"):
            new_award = (
                POINTS_CORRECT
                if prediction.choice == winning_side
                else POINTS_WRONG
            )
            delta = new_award - (prediction.points_awarded or 0)
            if delta:
                profile, _ = Profile.objects.get_or_create(user=prediction.user)
                Profile.objects.filter(pk=profile.pk).update(
                    points=F("points") + delta
                )
                prediction.points_awarded = new_award
                prediction.save(update_fields=["points_awarded"])
                changed = True

        if not match.is_scored:
            match.is_scored = True
            match.save(update_fields=["is_scored"])
            changed = True

        return changed
