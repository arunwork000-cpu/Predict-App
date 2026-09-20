import json

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .constants import DEFAULT_SPORT_SLUG, SPORT_SLUGS, SUPPORTED_SPORTS
from .forms import PredictionForm, RegistrationForm
from .locations import STATES_BY_COUNTRY
from .models import Match, Prediction, Profile, ScoreAdjustment


def _attach_user_picks(request, matches):
    """Set `.user_pick` on each match to the requesting user's Prediction, if any."""
    user_picks = {}
    if request.user.is_authenticated:
        user_picks = {
            p.match_id: p
            for p in Prediction.objects.filter(user=request.user, match__in=matches)
        }
    for match in matches:
        match.user_pick = user_picks.get(match.id)


def _sport_redirect(sport):
    """Redirect to the public sport page for `sport`, or the default page
    when it isn't one of the four supported public sports (e.g. test data)."""
    slug = sport.name.lower()
    if slug in SPORT_SLUGS:
        return redirect("sport_matches", sport_slug=slug)
    return redirect("match_list")


def home(request):
    """The generic homepage: it simply shows the default sport (Football)."""
    return sport_matches(request, DEFAULT_SPORT_SLUG)


def sport_matches(request, sport_slug):
    """Public page for one sport: only its published, currently-open matches."""
    sport_name = SPORT_SLUGS.get(sport_slug)
    if sport_name is None:
        raise Http404("Unknown sport.")

    matches = list(
        Match.objects.filter(is_published=True, sport__name=sport_name).select_related(
            "team_a", "team_b", "sport", "winner"
        )
    )
    open_matches = [m for m in matches if m.predictions_open]
    _attach_user_picks(request, open_matches)

    return render(
        request,
        "predictions/sport_matches.html",
        {
            "sport_name": sport_name,
            "sport_slug": sport_slug,
            "open_matches": open_matches,
        },
    )


def closed_matches_view(request):
    """Public page listing every published, no-longer-open match across the
    four supported sports (finished, live, cancelled, or deadline-passed)."""
    matches = list(
        Match.objects.filter(
            is_published=True, sport__name__in=SUPPORTED_SPORTS
        ).select_related("team_a", "team_b", "sport", "winner")
    )
    closed_matches = [m for m in matches if not m.predictions_open]
    closed_matches.sort(key=lambda m: m.start_time, reverse=True)
    _attach_user_picks(request, closed_matches)

    return render(
        request,
        "predictions/closed_matches.html",
        {"closed_matches": closed_matches},
    )


def match_detail(request, pk):
    """Read-only page for a single published match and its status."""
    match = get_object_or_404(
        Match.objects.select_related("sport", "team_a", "team_b", "winner"),
        pk=pk,
        is_published=True,
    )

    if match.status == Match.Status.CANCELLED:
        state = "cancelled"
    elif match.winner_id:
        state = "completed"
    elif match.status == Match.Status.LIVE:
        state = "live"
    elif match.predictions_open:
        state = "open"
    else:
        state = "locked"

    user_prediction = None
    if request.user.is_authenticated:
        user_prediction = Prediction.objects.filter(
            user=request.user, match=match
        ).first()

    return render(
        request,
        "predictions/match_detail.html",
        {
            "match": match,
            "state": state,
            "user_prediction": user_prediction,
        },
    )


@login_required
def predict(request, pk):
    match = get_object_or_404(Match.objects.select_related("sport"), pk=pk)
    existing = Prediction.objects.filter(user=request.user, match=match).first()

    if not match.predictions_open:
        messages.error(
            request,
            "Predictions are locked for this match. The deadline has passed or a winner is already set.",
        )
        return _sport_redirect(match.sport)

    if request.method == "POST":
        form = PredictionForm(request.POST, match=match)
        if form.is_valid():
            # Re-check deadline on the server; do not trust the form being visible.
            match.refresh_from_db()
            if not match.predictions_open:
                messages.error(
                    request,
                    "Predictions are locked for this match. The deadline has passed or a winner is already set.",
                )
                return _sport_redirect(match.sport)
            Prediction.objects.update_or_create(
                user=request.user,
                match=match,
                defaults={"choice": form.cleaned_data["choice"]},
            )
            messages.success(request, "Your prediction has been saved.")
            return _sport_redirect(match.sport)
    else:
        initial = {"choice": existing.choice} if existing else None
        form = PredictionForm(match=match, initial=initial)

    return render(
        request,
        "predictions/predict.html",
        {"match": match, "form": form, "existing": existing},
    )


@login_required
def my_predictions(request):
    predictions = (
        Prediction.objects.filter(user=request.user)
        .select_related(
            "match",
            "match__sport",
            "match__team_a",
            "match__team_b",
            "match__winner",
        )
    )
    cancelled = [
        p for p in predictions if p.match.status == Match.Status.CANCELLED
    ]
    active = [
        p for p in predictions if p.match.status != Match.Status.CANCELLED
    ]
    pending = [p for p in active if not p.match.is_scored]
    decided = [p for p in active if p.match.is_scored]
    pending.sort(key=lambda p: p.match.start_time)
    decided.sort(key=lambda p: p.match.start_time, reverse=True)
    cancelled.sort(key=lambda p: p.match.start_time, reverse=True)
    return render(
        request,
        "predictions/my_predictions.html",
        {"pending": pending, "decided": decided, "cancelled": cancelled},
    )


def _all_time_profiles():
    """Total points across all scored predictions, all time."""
    return Profile.objects.select_related("user").order_by(
        "-points", "user__username"
    )


def _monthly_profiles():
    """Points earned during the current calendar month.

    Uses the sum of this month's ScoreAdjustment rows rather than
    Profile.points, so a winner correction made this month for a match
    scored last month only contributes its net adjustment -- never the full
    original award again -- and re-running scoring with no change (delta 0)
    contributes nothing, matching score_match()'s existing idempotency.
    """
    # Fixed site zone (not the visitor's) so every user sees the same board.
    start_of_month = timezone.now().astimezone(timezone.get_default_timezone()).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    rows = (
        ScoreAdjustment.objects.filter(created_at__gte=start_of_month)
        .values("user_id")
        .annotate(total=Sum("delta"))
    )
    totals = {row["user_id"]: row["total"] for row in rows}

    profiles = list(Profile.objects.select_related("user"))
    for profile in profiles:
        profile.monthly_points = totals.get(profile.user_id, 0)
    profiles.sort(key=lambda p: (-p.monthly_points, p.user.username))
    return profiles


def leaderboard(request):
    """One page showing the All-Time and Monthly boards side by side."""
    return render(
        request,
        "predictions/leaderboard.html",
        {
            "all_time_profiles": _all_time_profiles(),
            "monthly_profiles": _monthly_profiles(),
        },
    )


def register(request):
    """Sign up with a custom form (adds required Country/State), then log in."""
    if request.user.is_authenticated:
        return redirect("match_list")
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            # A blank Profile row is created automatically (see signals.py);
            # fill in the location the form collected.
            profile = user.profile
            profile.country = form.cleaned_data["country"]
            profile.state = form.cleaned_data["state"]
            profile.save(update_fields=["country", "state"])
            login(request, user)
            messages.success(request, "Welcome. You can now make predictions.")
            return redirect("match_list")
    else:
        form = RegistrationForm()
    return render(
        request,
        "predictions/register.html",
        {"form": form, "states_by_country_json": json.dumps(STATES_BY_COUNTRY)},
    )
