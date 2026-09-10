from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PredictionForm
from .models import Match, Prediction, Profile


def match_list(request):
    matches = list(
        Match.objects.filter(is_published=True).select_related(
            "team_a", "team_b", "sport", "winner"
        )
    )
    user_picks = {}
    if request.user.is_authenticated:
        user_picks = {
            p.match_id: p
            for p in Prediction.objects.filter(
                user=request.user, match__in=matches
            )
        }
    for match in matches:
        match.user_pick = user_picks.get(match.id)
    open_matches = [m for m in matches if m.predictions_open]
    closed_matches = [m for m in matches if not m.predictions_open]
    return render(
        request,
        "predictions/match_list.html",
        {
            "open_matches": open_matches,
            "closed_matches": closed_matches,
        },
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
    match = get_object_or_404(Match, pk=pk)
    existing = Prediction.objects.filter(user=request.user, match=match).first()

    if not match.predictions_open:
        messages.error(
            request,
            "Predictions are locked for this match. The deadline has passed or a winner is already set.",
        )
        return redirect("match_list")

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
                return redirect("match_list")
            Prediction.objects.update_or_create(
                user=request.user,
                match=match,
                defaults={"choice": form.cleaned_data["choice"]},
            )
            messages.success(request, "Your prediction has been saved.")
            return redirect("match_list")
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
    pending = [p for p in predictions if not p.match.is_scored]
    decided = [p for p in predictions if p.match.is_scored]
    pending.sort(key=lambda p: p.match.start_time)
    decided.sort(key=lambda p: p.match.start_time, reverse=True)
    return render(
        request,
        "predictions/my_predictions.html",
        {"pending": pending, "decided": decided},
    )


def leaderboard(request):
    profiles = Profile.objects.select_related("user").order_by(
        "-points", "user__username"
    )
    return render(request, "predictions/leaderboard.html", {"profiles": profiles})


def register(request):
    """Sign up with Django's built-in UserCreationForm, then log the user in."""
    if request.user.is_authenticated:
        return redirect("match_list")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            # A Profile row is created automatically (see signals.py).
            login(request, user)
            messages.success(request, "Welcome. You can now make predictions.")
            return redirect("match_list")
    else:
        form = UserCreationForm()
    return render(request, "predictions/register.html", {"form": form})
