from django.db import transaction
from django.db.models import F

from .models import Match, Profile, ScoreAdjustment

# Legacy fallback values, kept for Prediction.points_earned's backward-compat
# path (rows scored before points_awarded existed). Live scoring now reads
# each match's own team_a/team_b win/lose point fields instead.
POINTS_CORRECT = 10
POINTS_WRONG = -5


def score_match(match_id):
    """Award or reconcile points for a match. Returns True if anything changed.

    Each prediction's award is stored on ``Prediction.points_awarded``, using
    the match's own configured win/lose points (``Match.points_for_choice``).
    Scoring only applies the *delta* between the new award and the value
    already stored, so:

    * the first run awards each pick its match-configured points;
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
                reversal = -prediction.points_awarded
                profile, _ = Profile.objects.get_or_create(user=prediction.user)
                Profile.objects.filter(pk=profile.pk).update(
                    points=F("points") + reversal
                )
                ScoreAdjustment.objects.create(
                    user=prediction.user, match=match, delta=reversal
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
            new_award = match.points_for_choice(prediction.choice)
            delta = new_award - (prediction.points_awarded or 0)
            if delta:
                profile, _ = Profile.objects.get_or_create(user=prediction.user)
                Profile.objects.filter(pk=profile.pk).update(
                    points=F("points") + delta
                )
                ScoreAdjustment.objects.create(
                    user=prediction.user, match=match, delta=delta
                )
                prediction.points_awarded = new_award
                prediction.save(update_fields=["points_awarded"])
                changed = True

        if not match.is_scored:
            match.is_scored = True
            match.save(update_fields=["is_scored"])
            changed = True

        return changed
