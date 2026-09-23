from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string

from .constants import DRAW_SPORTS

# Unambiguous alphabet for referral codes: no 0/O/1/I/L, so a code read
# aloud or typed by hand is never misread as a different valid code.
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 8


def generate_referral_code():
    """A unique, unambiguous referral code for a new Profile.

    Retries on the rare chance of a collision; used by the create_profile
    signal (see signals.py), not a model default, so it can query for
    uniqueness against existing rows.
    """
    for _ in range(10):
        code = get_random_string(
            REFERRAL_CODE_LENGTH, allowed_chars=REFERRAL_CODE_ALPHABET
        )
        if not Profile.objects.filter(referral_code=code).exists():
            return code
    raise RuntimeError("Could not generate a unique referral code.")


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    points = models.IntegerField(default=0)
    # Blank for legacy accounts created before this field existed, and for
    # any account created outside the registration form (e.g. createsuperuser).
    # Required at signup; enforced by RegistrationForm, not here.
    country = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    # Collected at signup (18-99); blank for accounts created before it existed.
    age = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(18), MaxValueValidator(99)],
    )
    # Referral wallet balance. Entirely separate from `points`: never read by
    # the leaderboard, never affected by scoring. See CreditLedger for the
    # audit trail of every change.
    credits = models.IntegerField(default=0)
    # Set once, at Profile creation (see signals.create_profile), and never
    # changed again. Blank only for rows created by a migration before this
    # field existed with a backfill still pending.
    referral_code = models.CharField(
        max_length=REFERRAL_CODE_LENGTH,
        unique=True,
        editable=False,
        default="",
    )

    class Meta:
        ordering = ["-points", "user__username"]

    def __str__(self):
        return f"{self.user.username} ({self.points} pts)"


class UserPredictionCount(Profile):
    """Proxy of Profile for the admin: a read-only "predictions per user"
    listing (this month / all-time), see UserPredictionCountAdmin."""

    class Meta:
        proxy = True
        verbose_name = "User Count"
        verbose_name_plural = "User Counts"


class Sport(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=100)
    sport = models.ForeignKey(Sport, on_delete=models.PROTECT, related_name="teams")
    flag = models.ImageField(
        upload_to="team_flags/",
        null=True,
        blank=True,
        help_text="Optional flag/logo shown next to the team name.",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["sport", "name"],
                name="unique_team_name_per_sport",
            )
        ]

    def __str__(self):
        return self.name


class Match(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        AWAITING_RESULT = "awaiting_result", "Awaiting result"
        FINISHED = "finished", "Finished"
        CANCELLED = "cancelled", "Cancelled"

    sport = models.ForeignKey(Sport, on_delete=models.PROTECT, related_name="matches")
    event_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Examples: World Cup, Euro Cup, Wimbledon.",
    )
    team_a = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="home_matches"
    )
    team_b = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="away_matches"
    )
    start_time = models.DateTimeField(help_text="When the match starts (kickoff).")
    prediction_deadline = models.DateTimeField(
        help_text="Last moment a prediction is allowed. Usually the same as kickoff."
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCHEDULED,
    )
    winner = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="won_matches",
        help_text="Leave empty until the match is over. Must be Team A or Team B.",
    )
    is_draw = models.BooleanField(
        default=False,
        help_text=(
            "Tick if the match ended in a draw (Football, Cricket and Hockey "
            "only). Leave Winner empty when this is ticked."
        ),
    )
    team_a_win_points = models.IntegerField(
        default=10,
        help_text="Points awarded to a user who picked Team A when Team A wins.",
    )
    team_a_lose_points = models.IntegerField(
        default=-5,
        help_text="Points awarded to a user who picked Team A when Team A loses.",
    )
    team_b_win_points = models.IntegerField(
        default=10,
        help_text="Points awarded to a user who picked Team B when Team B wins.",
    )
    team_b_lose_points = models.IntegerField(
        default=-5,
        help_text="Points awarded to a user who picked Team B when Team B loses.",
    )
    draw_win_points = models.IntegerField(
        default=10,
        help_text="Points awarded to a user who picked Draw when the match is a draw.",
    )
    draw_lose_points = models.IntegerField(
        default=-5,
        help_text="Points awarded to a user who picked Draw when the match is not a draw.",
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Unpublished matches stay hidden from the public list.",
    )
    # Used later when scoring predictions; not part of the public match form.
    is_scored = models.BooleanField(default=False)

    class Meta:
        ordering = ["start_time"]
        verbose_name_plural = "Matches"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(team_a=models.F("team_b")),
                name="match_team_a_ne_team_b",
            )
        ]

    def __str__(self):
        return f"{self.team_a} vs {self.team_b}"

    @property
    def predictions_open(self):
        return (
            self.is_published
            and timezone.now() < self.prediction_deadline
            and not self.has_result
            and self.status == self.Status.SCHEDULED
        )

    @property
    def allows_draw(self):
        """True if this match offers a Draw pick.

        Needs a sport where a match can end level, and Draw points other than
        0/0 - the admin sets both to 0 for a match that cannot be drawn.
        """
        if self.sport.name not in DRAW_SPORTS:
            return False
        return bool(self.draw_win_points or self.draw_lose_points)

    @property
    def has_result(self):
        """True once a winner or a draw has been entered."""
        return self.winner_id is not None or self.is_draw

    def winner_name(self):
        if self.is_draw:
            return "Draw"
        return str(self.winner) if self.winner_id else None

    def winning_side(self):
        """'A', 'B' or 'D' (draw) if a result is set, else None. Used by scoring."""
        if self.is_draw:
            return "D"
        if not self.winner_id:
            return None
        if self.winner_id == self.team_a_id:
            return "A"
        if self.winner_id == self.team_b_id:
            return "B"
        return None

    def points_for_choice(self, choice):
        """Points to award a prediction of `choice` ('A'/'B'/'D') for the current result.

        Uses this match's own configured win/lose points, so scoring is
        per-match rather than a single global constant.
        """
        winning_side = self.winning_side()
        if choice == "D":
            return self.draw_win_points if winning_side == "D" else self.draw_lose_points
        if choice == "A":
            return self.team_a_win_points if winning_side == "A" else self.team_a_lose_points
        return self.team_b_win_points if winning_side == "B" else self.team_b_lose_points

    def save(self, *args, **kwargs):
        # Entering a result (winner or draw) means the match is over, so a
        # scheduled/awaiting-result match moves to Finished automatically.
        # Cancelled and already-finished matches are left alone.
        if self.has_result and self.status in (
            self.Status.SCHEDULED,
            self.Status.AWAITING_RESULT,
        ):
            self.status = self.Status.FINISHED
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "status" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "status"]
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.team_a_id and self.team_b_id and self.team_a_id == self.team_b_id:
            errors["team_b"] = "Team A and Team B must be different."

        if self.sport_id and self.team_a_id:
            if self.team_a.sport_id != self.sport_id:
                errors["team_a"] = "Team A must play the same sport as this match."
        if self.sport_id and self.team_b_id:
            if self.team_b.sport_id != self.sport_id:
                errors["team_b"] = "Team B must play the same sport as this match."

        if self.winner_id and self.team_a_id and self.team_b_id:
            if self.winner_id not in (self.team_a_id, self.team_b_id):
                errors["winner"] = "Winner must be Team A or Team B."

        if self.is_draw:
            if self.winner_id:
                errors["is_draw"] = "A draw cannot also have a winner. Clear the winner."
            elif self.sport_id and not self.allows_draw:
                if self.sport.name in DRAW_SPORTS:
                    errors["is_draw"] = (
                        "This match has Draw points of 0 and 0, so it cannot "
                        "end in a draw."
                    )
                else:
                    errors["is_draw"] = (
                        f"{self.sport.name} matches cannot end in a draw."
                    )

        if (
            self.prediction_deadline
            and self.start_time
            and self.prediction_deadline > self.start_time
        ):
            errors["prediction_deadline"] = (
                "The prediction deadline cannot be after kickoff."
            )

        if errors:
            raise ValidationError(errors)


class Prediction(models.Model):
    class Side(models.TextChoices):
        A = "A", "Team A"
        B = "B", "Team B"
        DRAW = "D", "Draw"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="predictions",
    )
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="predictions",
    )
    choice = models.CharField(max_length=1, choices=Side.choices)
    points_awarded = models.IntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "Points recorded for this pick when the match was scored. "
            "None means the match has not been scored yet."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "match"],
                name="unique_prediction_per_user_match",
            )
        ]

    def __str__(self):
        return f"{self.user} → {self.match} ({self.choice})"

    def choice_name(self):
        if self.choice == self.Side.A:
            return str(self.match.team_a)
        if self.choice == self.Side.B:
            return str(self.match.team_b)
        if self.choice == self.Side.DRAW:
            return "Draw"
        return self.choice

    @property
    def is_decided(self):
        """True once the match has been scored (a result exists)."""
        return self.match.is_scored

    @property
    def is_correct(self):
        """True/False once the match is decided, else None."""
        if not self.match.is_scored:
            return None
        return self.choice == self.match.winning_side()

    @property
    def points_earned(self):
        """Points recorded for this pick, or None until the match is scored.

        Once scoring has run this is the value stored on the row
        (``points_awarded``). The rule-based fallback only covers rows that
        were scored before ``points_awarded`` existed.
        """
        if self.points_awarded is not None:
            return self.points_awarded
        if not self.match.is_scored:
            return None
        from .services import POINTS_CORRECT, POINTS_WRONG

        return (
            POINTS_CORRECT
            if self.choice == self.match.winning_side()
            else POINTS_WRONG
        )


class ScoreAdjustment(models.Model):
    """One net point change applied to a user's Profile by score_match().

    Recorded every time scoring changes ``Prediction.points_awarded`` (first
    scoring, a winner correction, or clearing a winner) -- the *delta*, not
    the absolute award, so summing these never double-counts a re-score.
    Profile.points is always the all-time total; summing this ledger's rows
    created within the current calendar month gives the monthly leaderboard.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="score_adjustments",
    )
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="score_adjustments",
    )
    delta = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sign = "+" if self.delta >= 0 else ""
        return f"{self.user} {sign}{self.delta} ({self.match})"


class Referral(models.Model):
    """One row per successful signup made with another user's referral code.

    `referred_user` is a OneToOneField so a user can be referred at most
    once -- a database-level guarantee, not just app logic. Credit is not
    awarded at creation: `status` stays "pending" until the referred user
    submits their first-ever prediction (see
    services.credit_referral_if_first_prediction), which is the anti-abuse
    signal this program relies on. Full fraud detection -- duplicate
    accounts, IP/device checks, velocity limits -- is out of scope.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CREDITED = "credited", "Credited"

    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referrals_made",
    )
    referred_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_used",
    )
    code_used = models.CharField(max_length=REFERRAL_CODE_LENGTH)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    credited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(referrer=models.F("referred_user")),
                name="referral_no_self_referral",
            ),
        ]

    def __str__(self):
        return f"{self.referrer} ← {self.referred_user} ({self.status})"


class ReferralSettings(models.Model):
    """Site-wide referral-program numbers, admin-configurable.

    Singleton: always saved/loaded at pk=1. Nothing else in this codebase
    has a generic settings row (per-match points live on Match itself), so
    this introduces the pattern for the referral program only.
    """

    credits_per_referral = models.PositiveIntegerField(
        default=10,
        help_text=(
            "Credits awarded to the referrer once the referred user "
            "submits their first prediction."
        ),
    )
    redemption_threshold = models.PositiveIntegerField(
        default=100,
        help_text="Credits required to redeem one voucher.",
    )

    class Meta:
        verbose_name = "Referral settings"
        verbose_name_plural = "Referral settings"

    def __str__(self):
        return "Referral settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class VoucherRedemption(models.Model):
    """A user's request to trade credits for a gift voucher.

    Fulfillment is manual: an admin arranges the actual voucher outside
    this system, then marks the request Fulfilled (or Rejected, which
    refunds the credits) via an admin action -- see admin.py and
    services.fulfill_redemption / reject_redemption.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FULFILLED = "fulfilled", "Fulfilled"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="voucher_redemptions",
    )
    # Always ReferralSettings.redemption_threshold at request time, not the
    # user's whole balance, so a user with enough credits for several
    # vouchers can redeem more than once.
    credits_spent = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional note, e.g. the voucher code/reference sent manually.",
    )

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.user} — {self.credits_spent} credits ({self.status})"


class CreditLedger(models.Model):
    """One row per change to a user's `Profile.credits`.

    Mirrors ScoreAdjustment: the *delta*, not the balance, so summing these
    for a user is always an accurate audit trail of the wallet, independent
    of `Profile.credits` itself.
    """

    class Reason(models.TextChoices):
        REFERRAL_EARNED = "referral_earned", "Referral earned"
        REDEMPTION_SPENT = "redemption_spent", "Redemption spent"
        REDEMPTION_REFUNDED = "redemption_refunded", "Redemption refunded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credit_adjustments",
    )
    delta = models.IntegerField()
    reason = models.CharField(max_length=20, choices=Reason.choices)
    referral = models.ForeignKey(
        Referral,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_entries",
    )
    redemption = models.ForeignKey(
        VoucherRedemption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sign = "+" if self.delta >= 0 else ""
        return f"{self.user} {sign}{self.delta} ({self.reason})"


class StoredFile(models.Model):
    """An uploaded file kept in the database (see predictions.storage)."""

    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    content_type = models.CharField(max_length=100, default="application/octet-stream")
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
