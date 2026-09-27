import os
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    CreditLedger,
    Match,
    Prediction,
    Profile,
    Referral,
    ReferralSettings,
    ScoreAdjustment,
    Team,
    TeamAlias,
    VoucherRedemption,
)

# Legacy fallback values, kept for Prediction.points_earned's backward-compat
# path (rows scored before points_awarded existed). Live scoring now reads
# each match's own team_a/team_b win/lose point fields instead.
POINTS_CORRECT = 10
POINTS_WRONG = -5


def sync_match_statuses():
    """Move Scheduled matches whose prediction deadline has passed (and that
    have no result yet) to Awaiting result, and move them back to Scheduled
    if the admin then pushes the deadline into the future again (e.g. a
    delayed kickoff) before any result is entered -- so predictions reopen
    automatically. Returns the total number of matches updated, either
    direction.

    Cheap and idempotent (two conditional UPDATEs), so it is called wherever
    match status is shown -- public match pages and the admin -- instead of
    needing a background job.
    """
    moved_to_awaiting = Match.objects.filter(
        status=Match.Status.SCHEDULED,
        prediction_deadline__lte=timezone.now(),
        winner__isnull=True,
        is_draw=False,
    ).update(status=Match.Status.AWAITING_RESULT)

    moved_to_scheduled = Match.objects.filter(
        status=Match.Status.AWAITING_RESULT,
        prediction_deadline__gt=timezone.now(),
        winner__isnull=True,
        is_draw=False,
    ).update(status=Match.Status.SCHEDULED)

    return moved_to_awaiting + moved_to_scheduled


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


def _resolve_team(provider, sport, name, image_url, summary, download_images):
    """The Team called `name` in `sport`, creating it if it doesn't exist.

    Team names are entered exactly as Flashscore spells them, so an exact
    match is the norm; TeamAlias covers the odd name that differs.
    """
    name = name[:100]
    team = (
        Team.objects.filter(sport=sport, name=name).first()
        or Team.objects.filter(sport=sport, name__iexact=name).first()
    )
    if team:
        return team
    alias = (
        TeamAlias.objects.filter(
            source=provider.name, external_name=name, team__sport=sport
        )
        .select_related("team")
        .first()
    )
    if alias:
        return alias.team

    team = Team.objects.create(name=name, sport=sport)
    summary["new_teams"].append(f"{sport.name}: {name}")
    if download_images and image_url:
        data = provider.fetch_image(image_url)
        if data:
            extension = os.path.splitext(image_url.split("?")[0])[1].lower()
            if extension not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                extension = ".png"
            team.flag.save(
                f"{slugify(name) or 'team'}{extension}", ContentFile(data), save=True
            )
    return team


def import_fixtures(
    provider, sport, days_ahead=7, download_images=True, new_day_only=False
):
    """Create (unpublished) matches for `sport`'s upcoming fixtures from
    `provider`, and follow kickoff-time changes of matches already imported.

    Idempotent: matches are keyed on (external_source, external_id). New
    matches start unpublished, so an admin reviews them and publishes with
    the "Publish selected matches" action. Returns a summary dict.
    """
    summary = {"created": 0, "updated": 0, "skipped": 0, "new_teams": [], "called_off": []}
    now = timezone.now()

    for event in provider.fetch_fixtures(
        sport.name, days_ahead, new_day_only=new_day_only
    ):
        match = Match.objects.filter(
            external_source=event.source, external_id=event.external_id
        ).first()

        if match is None:
            if event.called_off or event.result or event.start_time <= now:
                summary["skipped"] += 1
                continue
            team_a = _resolve_team(
                provider, sport, event.home, event.home_image, summary, download_images
            )
            team_b = _resolve_team(
                provider, sport, event.away, event.away_image, summary, download_images
            )
            if team_a == team_b:
                summary["skipped"] += 1
                continue
            Match.objects.create(
                sport=sport,
                event_name=event.event_name,
                team_a=team_a,
                team_b=team_b,
                start_time=event.start_time,
                prediction_deadline=event.start_time,
                is_published=False,
                external_source=event.source,
                external_id=event.external_id,
            )
            summary["created"] += 1
            continue

        if event.called_off:
            summary["called_off"].append(str(match))
        elif (
            match.status == Match.Status.SCHEDULED
            and not match.has_result
            and match.start_time != event.start_time
        ):
            # Kickoff moved: keep the admin's deadline offset from kickoff.
            offset = match.start_time - match.prediction_deadline
            match.start_time = event.start_time
            match.prediction_deadline = event.start_time - offset
            match.save(update_fields=["start_time", "prediction_deadline"])
            summary["updated"] += 1

    return summary


# Imported matches are only looked up for this long after kickoff; after
# that the admin enters the result by hand. Stops a match the source never
# settles (e.g. a cricket result with no winner) using API quota every run.
RESULT_LOOKUP_DAYS = 3


def sync_results(provider, sport):
    """Store `provider`'s final results as *suggested* results on imported
    `sport` matches awaiting one.

    Never sets winner/is_draw and never scores: an admin confirms each
    suggestion (confirm_suggested_result). Returns a summary dict.
    """
    sync_match_statuses()
    summary = {"suggested": 0, "called_off": [], "enter_by_hand": []}
    cutoff = timezone.now() - timedelta(days=RESULT_LOOKUP_DAYS)
    pending = {}
    for match in (
        Match.objects.filter(
            sport=sport,
            external_source=provider.name,
            status=Match.Status.AWAITING_RESULT,
            winner__isnull=True,
            is_draw=False,
            suggested_at__isnull=True,
        )
        .exclude(external_id="")
        .select_related("team_a", "team_b")
    ):
        if match.start_time < cutoff:
            summary["enter_by_hand"].append(str(match))
        else:
            pending[match.external_id] = match
    if not pending:
        return summary

    kickoffs = {external_id: m.start_time for external_id, m in pending.items()}
    for external_id, event in provider.fetch_results(sport.name, kickoffs).items():
        match = pending.get(external_id)
        if match is None:
            continue
        if event.called_off:
            summary["called_off"].append(str(match))
            continue
        if event.result is None:
            continue
        match.suggested_winner = {"home": match.team_a, "away": match.team_b}.get(
            event.result
        )
        match.suggested_is_draw = event.result == "draw"
        match.suggested_at = timezone.now()
        match.save(
            update_fields=["suggested_winner", "suggested_is_draw", "suggested_at"]
        )
        summary["suggested"] += 1

    return summary


def confirm_suggested_result(match):
    """Make `match`'s suggested result its real result and score it.

    Raises ValidationError if there is no suggestion, the match already has
    a result, or the suggestion is invalid (e.g. a draw on a match that
    can't be drawn -- the same checks as entering a result by hand).
    """
    if not match.has_suggested_result:
        raise ValidationError("There is no suggested result to confirm.")
    if match.has_result or match.status not in (
        Match.Status.SCHEDULED,
        Match.Status.AWAITING_RESULT,
    ):
        raise ValidationError("This match already has a result or is cancelled.")
    match.winner = match.suggested_winner
    match.is_draw = match.suggested_is_draw
    match.full_clean()
    match.save()  # moves the match to Finished
    score_match(match.pk)


def apply_referral_code(referred_user, code):
    """Create a pending Referral for `referred_user` if `code` is a real
    referral code, called right after registration.

    Silently no-ops on any problem (unknown code, self-referral, already
    referred) rather than raising: RegistrationForm.clean_referral_code()
    already rejects an unknown code before this can be reached, so this
    stays defensive rather than load-bearing.
    """
    if not code:
        return
    referrer_profile = (
        Profile.objects.filter(referral_code=code).select_related("user").first()
    )
    if referrer_profile is None or referrer_profile.user_id == referred_user.id:
        return
    Referral.objects.get_or_create(
        referred_user=referred_user,
        defaults={"referrer": referrer_profile.user, "code_used": code},
    )


def credit_referral_if_first_prediction(user):
    """Credit `user`'s referrer, once, the first time `user` ever predicts.

    No-ops for users with no referral, or whose referral is already
    credited. Race-safe: concurrent calls for the same referred user
    serialise on the Referral row lock, so only one can win -- the same
    select_for_update + re-check pattern as score_match().
    """
    with transaction.atomic():
        referral = (
            Referral.objects.select_for_update()
            .filter(referred_user=user)
            .first()
        )
        if referral is None or referral.status != Referral.Status.PENDING:
            return
        if Prediction.objects.filter(user=user).count() != 1:
            return  # not their first prediction ever

        amount = ReferralSettings.load().credits_per_referral
        Profile.objects.filter(user_id=referral.referrer_id).update(
            credits=F("credits") + amount
        )
        CreditLedger.objects.create(
            user_id=referral.referrer_id,
            delta=amount,
            reason=CreditLedger.Reason.REFERRAL_EARNED,
            referral=referral,
        )
        referral.status = Referral.Status.CREDITED
        referral.credited_at = timezone.now()
        referral.save(update_fields=["status", "credited_at"])

    send_referral_credited_email(referral)


def redeem_credits(user):
    """Spend one voucher's worth of credits and open a pending redemption
    request. Returns the VoucherRedemption, or None if the user's balance
    is below the current threshold.
    """
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        threshold = ReferralSettings.load().redemption_threshold
        if profile.credits < threshold:
            return None
        Profile.objects.filter(pk=profile.pk).update(
            credits=F("credits") - threshold
        )
        redemption = VoucherRedemption.objects.create(
            user=user, credits_spent=threshold
        )
        CreditLedger.objects.create(
            user=user,
            delta=-threshold,
            reason=CreditLedger.Reason.REDEMPTION_SPENT,
            redemption=redemption,
        )
        return redemption


def fulfill_redemption(redemption_id):
    """Mark a pending redemption Fulfilled. Returns False if it wasn't
    pending (already resolved), so admin actions can no-op safely."""
    with transaction.atomic():
        redemption = VoucherRedemption.objects.select_for_update().get(
            pk=redemption_id
        )
        if redemption.status != VoucherRedemption.Status.PENDING:
            return False
        redemption.status = VoucherRedemption.Status.FULFILLED
        redemption.resolved_at = timezone.now()
        redemption.save(update_fields=["status", "resolved_at"])

    send_redemption_fulfilled_email(redemption)
    return True


def reject_redemption(redemption_id):
    """Reject a pending redemption and refund its credits. Returns False
    if it wasn't pending (already resolved)."""
    with transaction.atomic():
        redemption = VoucherRedemption.objects.select_for_update().get(
            pk=redemption_id
        )
        if redemption.status != VoucherRedemption.Status.PENDING:
            return False
        redemption.status = VoucherRedemption.Status.REJECTED
        redemption.resolved_at = timezone.now()
        redemption.save(update_fields=["status", "resolved_at"])
        Profile.objects.filter(user_id=redemption.user_id).update(
            credits=F("credits") + redemption.credits_spent
        )
        CreditLedger.objects.create(
            user_id=redemption.user_id,
            delta=redemption.credits_spent,
            reason=CreditLedger.Reason.REDEMPTION_REFUNDED,
            redemption=redemption,
        )
    return True


def send_referral_credited_email(referral):
    """Best-effort notification to the referrer once their referral
    converts. No-ops silently if they have no email on file."""
    user = referral.referrer
    if not user.email:
        return
    subject = render_to_string(
        "predictions/email/referral_credited_subject.txt", {"referral": referral}
    ).strip()
    message = render_to_string(
        "predictions/email/referral_credited_email.html",
        {"referral": referral, "user": user},
    )
    send_mail(subject, message, None, [user.email])


def send_redemption_fulfilled_email(redemption):
    """Best-effort notification once a redemption is marked Fulfilled.
    No-ops silently if the user has no email on file."""
    user = redemption.user
    if not user.email:
        return
    subject = render_to_string(
        "predictions/email/redemption_fulfilled_subject.txt",
        {"redemption": redemption},
    ).strip()
    message = render_to_string(
        "predictions/email/redemption_fulfilled_email.html",
        {"redemption": redemption, "user": user},
    )
    send_mail(subject, message, None, [user.email])
