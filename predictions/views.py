import datetime
import json

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .constants import DEFAULT_SPORT_SLUG, DRAW_SPORTS, SPORT_SLUGS, SUPPORTED_SPORTS
from .forms import AddEmailForm, PredictionForm, RegistrationForm
from .locations import STATES_BY_COUNTRY
from .models import (
    Match,
    Prediction,
    Profile,
    Referral,
    ReferralSettings,
    ScoreAdjustment,
    StoredFile,
    VoucherRedemption,
)
from .services import (
    apply_referral_code,
    credit_referral_if_first_prediction,
    redeem_credits,
    sync_match_statuses,
)


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

    sync_match_statuses()
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
            "draw_sports": DRAW_SPORTS,
        },
    )


def closed_matches_view(request):
    """Public page listing every published, no-longer-open match across the
    four supported sports (finished, awaiting result, cancelled, or deadline-passed)."""
    sync_match_statuses()
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
    sync_match_statuses()
    match = get_object_or_404(
        Match.objects.select_related("sport", "team_a", "team_b", "winner"),
        pk=pk,
        is_published=True,
    )

    if match.status == Match.Status.CANCELLED:
        state = "cancelled"
    elif match.has_result:
        state = "completed"
    elif match.status == Match.Status.AWAITING_RESULT:
        state = "awaiting"
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
            credit_referral_if_first_prediction(request.user)
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
    sync_match_statuses()
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


@login_required
def my_account(request):
    """Referral link, credit wallet and redemption history for the current
    user. `profile.credits` is entirely separate from `profile.points`
    (leaderboard) -- see Profile/CreditLedger in models.py."""
    profile = request.user.profile
    settings_row = ReferralSettings.load()
    threshold = settings_row.redemption_threshold
    referral_link = request.build_absolute_uri(
        f"{reverse('register')}?ref={profile.referral_code}"
    )
    referrals = Referral.objects.filter(referrer=request.user).select_related(
        "referred_user"
    )
    return render(
        request,
        "predictions/my_account.html",
        {
            "profile": profile,
            "referral_link": referral_link,
            "referrals": referrals,
            "referrals_credited": sum(
                1 for r in referrals if r.status == Referral.Status.CREDITED
            ),
            "redemptions": VoucherRedemption.objects.filter(user=request.user),
            "credits_threshold": threshold,
            "progress_pct": min(100, profile.credits * 100 // threshold)
            if threshold
            else 0,
            "can_redeem": profile.credits >= threshold,
        },
    )


@login_required
def redeem_credits_view(request):
    if request.method == "POST":
        redemption = redeem_credits(request.user)
        if redemption is None:
            messages.error(request, "You don't have enough credits to redeem yet.")
        else:
            messages.success(
                request,
                "Redemption requested — we'll be in touch once it's fulfilled.",
            )
    return redirect("my_account")


def _all_time_profiles():
    """Total points across all scored predictions, all time."""
    return Profile.objects.select_related("user").order_by(
        "-points", "user__username"
    )


def _current_month():
    """(year, month) of now, in the fixed site zone (not the visitor's) so
    every user sees the same board."""
    now = timezone.now().astimezone(timezone.get_default_timezone())
    return now.year, now.month


def _parse_month(value):
    """Parse a `YYYY-MM` query value; None if missing or malformed."""
    try:
        year_str, month_str = (value or "").split("-")
        year, month = int(year_str), int(month_str)
        datetime.date(year, month, 1)
    except (ValueError, TypeError):
        return None
    return year, month


def _monthly_profiles(year, month):
    """Points earned during the given calendar month.

    Uses the sum of that month's ScoreAdjustment rows rather than
    Profile.points, so a winner correction made this month for a match
    scored last month only contributes its net adjustment -- never the full
    original award again -- and re-running scoring with no change (delta 0)
    contributes nothing, matching score_match()'s existing idempotency.
    """
    zone = timezone.get_default_timezone()
    start = datetime.datetime(year, month, 1, tzinfo=zone)
    end = (
        datetime.datetime(year + 1, 1, 1, tzinfo=zone)
        if month == 12
        else datetime.datetime(year, month + 1, 1, tzinfo=zone)
    )
    rows = (
        ScoreAdjustment.objects.filter(created_at__gte=start, created_at__lt=end)
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
    """One page showing the All-Time and Monthly boards side by side.

    The Monthly board defaults to the current month (full ranking). `?month=
    YYYY-MM` picks an earlier month, which shows only its top 3.
    """
    current = _current_month()
    selected = _parse_month(request.GET.get("month"))
    if selected is None or selected > current:
        selected = current
    is_past_month = selected != current

    monthly_profiles = _monthly_profiles(*selected)
    if is_past_month:
        # Only the winners: a zero-point player isn't one.
        monthly_profiles = [p for p in monthly_profiles if p.monthly_points > 0][:3]

    zone = timezone.get_default_timezone()
    months_with_activity = {
        (m.year, m.month)
        for m in ScoreAdjustment.objects.annotate(
            month=TruncMonth("created_at", tzinfo=zone)
        )
        .values_list("month", flat=True)
        .distinct()
    }
    months_with_activity.add(current)
    month_options = [
        {
            "value": f"{year:04d}-{month:02d}",
            "label": datetime.date(year, month, 1).strftime("%B %Y"),
        }
        for year, month in sorted(months_with_activity, reverse=True)
        if (year, month) <= current
    ]

    return render(
        request,
        "predictions/leaderboard.html",
        {
            "all_time_profiles": _all_time_profiles(),
            "monthly_profiles": monthly_profiles,
            "is_past_month": is_past_month,
            "selected_month": f"{selected[0]:04d}-{selected[1]:02d}",
            "month_options": month_options,
        },
    )


def register(request):
    """Sign up with a custom form (required Email, Country, State and Age), then log in."""
    if request.user.is_authenticated:
        return redirect("match_list")
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            # A blank Profile row is created automatically (see signals.py);
            # fill in the location and age the form collected.
            profile = user.profile
            profile.country = form.cleaned_data["country"]
            profile.state = form.cleaned_data["state"]
            profile.age = form.cleaned_data["age"]
            profile.save(update_fields=["country", "state", "age"])
            apply_referral_code(user, form.cleaned_data["referral_code"])
            login(request, user)
            messages.success(request, "Welcome. You can now make predictions.")
            return redirect("match_list")
    else:
        # A referral link looks like /accounts/register/?ref=<code>; prefill
        # the field so the visitor doesn't have to retype it.
        form = RegistrationForm(initial={"referral_code": request.GET.get("ref", "")})
    return render(
        request,
        "predictions/register.html",
        {"form": form, "states_by_country_json": json.dumps(STATES_BY_COUNTRY)},
    )


@login_required
def add_email(request):
    """One-time page asking a logged-in user with no email for one.

    EmailRequiredMiddleware sends such users here; once an email is saved
    they are sent on to the page they were trying to reach.
    """
    next_url = request.GET.get("next") or request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = ""
    if request.user.email:
        return redirect(next_url or "match_list")
    if request.method == "POST":
        form = AddEmailForm(request.POST, user=request.user)
        if form.is_valid():
            request.user.email = form.cleaned_data["email"]
            request.user.save(update_fields=["email"])
            messages.success(request, "Thanks, your email has been saved.")
            return redirect(next_url or "match_list")
    else:
        form = AddEmailForm(user=request.user)
    return render(
        request, "registration/add_email.html", {"form": form, "next": next_url}
    )


def media_file(request, path):
    """Serve an uploaded file (e.g. a team flag) stored in the database."""
    stored = get_object_or_404(StoredFile, name=path)
    response = HttpResponse(bytes(stored.content), content_type=stored.content_type)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response
