import datetime
import json
import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import google_auth, push
from .constants import DEFAULT_SPORT_SLUG, DRAW_SPORTS, SPORT_SLUGS, SUPPORTED_SPORTS
from .forms import AddEmailForm, LocationForm, PredictionForm, RegistrationForm
from .locations import STATES_BY_COUNTRY
from .models import (
    Match,
    Prediction,
    Profile,
    PushSubscription,
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

logger = logging.getLogger(__name__)


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
    # Already-predicted matches sink below the still-open ones, so after the
    # full-page reload a submitted prediction causes, the next match to
    # predict is near the top instead of buried below ones already done.
    open_matches.sort(key=lambda m: (m.user_pick is not None, m.start_time))
    # The most recently saved pick goes second, just below the next match to
    # predict, so the user can still see it without scrolling.
    predicted = [m for m in open_matches if m.user_pick is not None]
    if predicted:
        latest = max(predicted, key=lambda m: m.user_pick.updated_at)
        open_matches.remove(latest)
        position = 1 if open_matches and open_matches[0].user_pick is None else 0
        open_matches.insert(position, latest)

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
    user, plus the optional Country / State shown on the leaderboard (POST
    saves those). `profile.credits` is entirely separate from
    `profile.points` (leaderboard) -- see Profile/CreditLedger in models.py."""
    profile = request.user.profile
    if request.method == "POST":
        location_form = LocationForm(request.POST, instance=profile)
        if location_form.is_valid():
            location_form.save()
            messages.success(request, "Your location has been saved.")
            return redirect("my_account")
    else:
        location_form = LocationForm(instance=profile)
    settings_row = ReferralSettings.load()
    threshold = settings_row.redemption_threshold
    referral_link = request.build_absolute_uri(
        f"{reverse('register')}?ref={profile.referral_code}"
    )
    referrals = Referral.objects.filter(referrer=request.user).select_related(
        "referred_user"
    )

    all_time_rank = Profile.objects.filter(
        Q(points__gt=profile.points)
        | Q(points=profile.points, user__username__lt=profile.user.username)
    ).count() + 1

    monthly_points, monthly_rank = 0, None
    for i, p in enumerate(_monthly_profiles(*_current_month()), start=1):
        if p.user_id == request.user.id:
            monthly_points, monthly_rank = p.monthly_points, i
            break

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
            "credits_per_referral": settings_row.credits_per_referral,
            "redemptions": VoucherRedemption.objects.filter(user=request.user),
            "credits_threshold": threshold,
            "progress_pct": min(100, profile.credits * 100 // threshold)
            if threshold
            else 0,
            "can_redeem": profile.credits >= threshold,
            "all_time_points": profile.points,
            "all_time_rank": all_time_rank,
            "monthly_points": monthly_points,
            "monthly_rank": monthly_rank,
            "location_form": location_form,
            "states_by_country_json": json.dumps(STATES_BY_COUNTRY),
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
    return (
        Profile.objects.select_related("user")
        .annotate(predictions_count=Count("user__predictions"))
        .order_by("-points", "user__username")
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

    prediction_rows = (
        Prediction.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("user_id")
        .annotate(total=Count("id"))
    )
    prediction_totals = {row["user_id"]: row["total"] for row in prediction_rows}

    profiles = list(Profile.objects.select_related("user"))
    for profile in profiles:
        profile.monthly_points = totals.get(profile.user_id, 0)
        profile.monthly_predictions_count = prediction_totals.get(profile.user_id, 0)
    profiles.sort(key=lambda p: (-p.monthly_points, p.user.username))
    return profiles


MEDAL_ELIGIBILITY_START_DAY = 21  # day-of-month the 100-prediction floor kicks in
MEDAL_MIN_PREDICTIONS = 100
_MEDALS = ("gold", "silver", "bronze")


def _assign_medals_by_rank(profiles):
    """Top 3 by position get gold/silver/bronze; no eligibility check."""
    for profile in profiles:
        profile.medal = None
    for medal, profile in zip(_MEDALS, profiles):
        profile.medal = medal


def _assign_medals_by_eligibility(profiles, minimum, count_attr="monthly_predictions_count"):
    """Top 3 by position *among those with >= minimum predictions on this
    board* (read from `count_attr`) get gold/silver/bronze; a higher-ranked
    but ineligible player is skipped, not just left medal-less in their slot."""
    for profile in profiles:
        profile.medal = None
    eligible = (p for p in profiles if getattr(p, count_attr) >= minimum)
    for medal, profile in zip(_MEDALS, eligible):
        profile.medal = medal


def leaderboard(request):
    """One page showing the All-Time and Monthly boards side by side.

    The Monthly board defaults to the current month (full ranking). `?month=
    YYYY-MM` picks an earlier month, which shows only its top 3.
    """
    current = _current_month()
    # Temporary promo for the October 2026 voucher draw; gone for good once
    # November 2026 starts. Remove this once it's no longer needed.
    show_voucher_announcement = current <= (2026, 10)
    selected = _parse_month(request.GET.get("month"))
    if selected is None or selected > current:
        selected = current
    is_past_month = selected != current
    zone = timezone.get_default_timezone()

    # All-Time medals always need the prediction floor; no day-of-month rule.
    all_time_profiles = list(_all_time_profiles())
    _assign_medals_by_eligibility(
        all_time_profiles, MEDAL_MIN_PREDICTIONS, count_attr="predictions_count"
    )

    monthly_profiles = _monthly_profiles(*selected)
    if is_past_month:
        # Only the winners: a zero-point player isn't one. Past months are
        # already decided, so medals stay purely rank-based here.
        monthly_profiles = [p for p in monthly_profiles if p.monthly_points > 0][:3]
        _assign_medals_by_rank(monthly_profiles)
    else:
        today = timezone.localtime(timezone.now(), zone).day
        if today >= MEDAL_ELIGIBILITY_START_DAY:
            _assign_medals_by_eligibility(monthly_profiles, MEDAL_MIN_PREDICTIONS)
        else:
            _assign_medals_by_rank(monthly_profiles)

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
            "all_time_profiles": all_time_profiles,
            "monthly_profiles": monthly_profiles,
            "is_past_month": is_past_month,
            "selected_month": f"{selected[0]:04d}-{selected[1]:02d}",
            "month_options": month_options,
            "show_voucher_announcement": show_voucher_announcement,
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
    pending = request.session.get(GOOGLE_PENDING_KEY)
    return render(
        request,
        "predictions/register.html",
        {
            "form": form,
            "states_by_country_json": json.dumps(STATES_BY_COUNTRY),
            "ref": request.GET.get("ref", ""),
            "google_pending_email": pending["identity"]["email"] if pending else "",
        },
    )


# Session keys for the Google sign-in flow.
GOOGLE_FLOW_KEY = "google_oauth"
# A verified Google identity with no account yet, waiting for the visitor to
# accept the Terms on the Register page (they started from Log in).
GOOGLE_PENDING_KEY = "google_pending"


def _safe_next(request, url):
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return ""


@require_POST
def google_start(request):
    """Send the visitor to Google's account chooser.

    The form on the Log in / Register page posts here; the Register one also
    carries the required accept_terms checkbox and any referral code, which
    are kept in the session until Google sends the visitor back.
    """
    if not google_auth.is_enabled():
        raise Http404
    if request.user.is_authenticated:
        return redirect("match_list")
    accept_terms = request.POST.get("accept_terms") == "on"
    ref = request.POST.get("ref", "").strip().upper()[:10]
    next_url = _safe_next(request, request.POST.get("next", ""))

    # Google already vouched for this visitor (see google_callback); only
    # the Terms were missing, so finish without another trip to Google.
    pending = request.session.get(GOOGLE_PENDING_KEY)
    if pending and accept_terms:
        return _google_login(
            request,
            pending["identity"],
            accept_terms=True,
            ref=ref or pending.get("ref", ""),
            next_url=next_url or pending.get("next", ""),
        )

    state = secrets.token_urlsafe(32)
    request.session[GOOGLE_FLOW_KEY] = {
        "state": state,
        "accept_terms": accept_terms,
        "ref": ref,
        "next": next_url,
    }
    return redirect(google_auth.build_auth_url(request, state))


def google_callback(request):
    """Where Google sends the visitor back after they pick an account."""
    if not google_auth.is_enabled():
        raise Http404
    flow = request.session.pop(GOOGLE_FLOW_KEY, None)
    if request.GET.get("error"):
        messages.info(request, "Google sign-in was cancelled.")
        return redirect("login")
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if not flow or not code or not secrets.compare_digest(flow["state"], state):
        messages.error(request, "Google sign-in expired or was invalid. Please try again.")
        return redirect("login")
    try:
        identity = google_auth.exchange_code(request, code)
    except google_auth.GoogleAuthError as exc:
        logger.warning("Google sign-in failed: %s", exc)
        messages.error(request, "We couldn't sign you in with Google. Please try again.")
        return redirect("login")
    return _google_login(
        request,
        identity,
        accept_terms=flow["accept_terms"],
        ref=flow["ref"],
        next_url=flow["next"],
    )


def _google_login(request, identity, *, accept_terms, ref, next_url):
    """Log in (creating the account if needed) the owner of a verified
    Google identity: matched by Google ID first, then by email."""
    profile = (
        Profile.objects.filter(google_sub=identity["sub"]).select_related("user").first()
    )
    user = profile.user if profile else None
    created = False

    if user is None:
        by_email = list(User.objects.filter(email__iexact=identity["email"])[:2])
        if len(by_email) > 1:
            # Legacy data: two accounts share this email. Don't guess.
            messages.error(
                request,
                "More than one account uses this email. Please log in with "
                "your username and password.",
            )
            return redirect("login")
        if by_email:
            # Google verified the address, so it's the same person: link it.
            user = by_email[0]
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.google_sub = identity["sub"]
            profile.save(update_fields=["google_sub"])

    if user is None:
        if not accept_terms:
            request.session[GOOGLE_PENDING_KEY] = {
                "identity": identity,
                "ref": ref,
                "next": next_url,
            }
            messages.info(
                request,
                "Almost there: tick the box to agree to the Terms, then "
                "continue with Google to create your account.",
            )
            return redirect("register")
        with transaction.atomic():
            # password=None gives an unusable password: Google-only login.
            user = User.objects.create_user(
                username=google_auth.generate_username(identity["name"]),
                email=identity["email"],
                password=None,
            )
            profile = user.profile  # created by the post_save signal
            profile.google_sub = identity["sub"]
            profile.save(update_fields=["google_sub"])
            apply_referral_code(user, ref)
        created = True

    request.session.pop(GOOGLE_PENDING_KEY, None)
    if not user.is_active:
        messages.error(request, "This account has been deactivated.")
        return redirect("login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if created:
        messages.success(
            request,
            f"Welcome, {user.username}. You can now make predictions. To show "
            "your state and country on the leaderboard, add them in My Account.",
        )
    return redirect(next_url or "match_list")


def terms(request):
    return render(request, "predictions/terms.html")


def privacy(request):
    return render(request, "predictions/privacy.html")


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


def web_manifest(request):
    """The installable-app manifest (icons, name, theme colour)."""
    return render(
        request,
        "pwa/manifest.webmanifest",
        content_type="application/manifest+json",
    )


def service_worker(request):
    """The app's service worker, served from the site root so it controls
    every page (a worker under /static/ could only control /static/)."""
    response = render(
        request,
        "pwa/sw.js",
        {"static_url": settings.STATIC_URL},
        content_type="application/javascript",
    )
    response["Cache-Control"] = "no-cache"
    return response


def offline(request):
    """Shown by the service worker when a page can't be reached."""
    return render(request, "pwa/offline.html")


@login_required
@require_POST
def push_subscribe(request):
    """Save this browser's push subscription for the logged-in user (match
    alerts, see predictions.push). pwa.js sends it again on every page, so
    a device that logs in to another account moves to that account."""
    if not push.is_enabled():
        raise Http404
    try:
        data = json.loads(request.body)
        endpoint = data["endpoint"]
        keys = data["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False}, status=400)
    if not (isinstance(endpoint, str) and endpoint.startswith("https://")) or len(endpoint) > 1000:
        return JsonResponse({"ok": False}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": str(p256dh)[:200], "auth": str(auth)[:100]},
    )
    return JsonResponse({"ok": True})


@require_POST
def push_unsubscribe(request):
    """Forget a push subscription (alerts turned off on this device)."""
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False}, status=400)
    if request.user.is_authenticated:
        PushSubscription.objects.filter(endpoint=endpoint, user=request.user).delete()
    return JsonResponse({"ok": True})
