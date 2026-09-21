from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .constants import DRAW_SPORTS


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

    class Meta:
        ordering = ["-points", "user__username"]

    def __str__(self):
        return f"{self.user.username} ({self.points} pts)"


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
