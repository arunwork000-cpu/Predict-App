from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .locations import COUNTRY_CHOICES, STATE_CHOICES, states_for
from .models import Match, Prediction


MIN_AGE = 18
MAX_AGE = 99


class RegistrationForm(UserCreationForm):
    """UserCreationForm plus required Email, Country, State and Age.

    Both fields are ChoiceFields (rendered as <select>), so a submission can
    only carry one of the values we listed -- never free text. The state
    list is deliberately every state across every supported country (the
    dependent-dropdown filtering is a client-side convenience); the actual
    country/state match is enforced in clean() below.
    """

    email = forms.EmailField(
        label="Email",
        help_text=(
            "Required. Used to reset your password and to contact prize "
            "winners."
        ),
    )
    country = forms.ChoiceField(
        choices=[("", "Select a country")] + COUNTRY_CHOICES,
        label="Country",
    )
    state = forms.ChoiceField(
        choices=[("", "Select a country first")] + STATE_CHOICES,
        label="State / Province / Region",
    )
    age = forms.TypedChoiceField(
        choices=[("", "Select your age")]
        + [(a, str(a)) for a in range(MIN_AGE, MAX_AGE + 1)],
        coerce=int,
        empty_value=None,
        label="Age",
    )

    field_order = [
        "username",
        "email",
        "password1",
        "password2",
        "country",
        "state",
        "age",
    ]

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self):
        return unique_email(self.cleaned_data["email"])

    def clean(self):
        cleaned_data = super().clean()
        country = cleaned_data.get("country")
        state = cleaned_data.get("state")
        if country and state and state not in states_for(country):
            self.add_error(
                "state", "Select a state that belongs to the chosen country."
            )
        return cleaned_data


def unique_email(email, exclude_user=None):
    """Return `email` lower-cased, or raise ValidationError if another
    account already uses it (compared case-insensitively).

    auth_user.email has no DB unique constraint, so this is what keeps a
    password-reset email pointing at exactly one account.
    """
    email = email.strip().lower()
    taken = User.objects.filter(email__iexact=email)
    if exclude_user is not None:
        taken = taken.exclude(pk=exclude_user.pk)
    if taken.exists():
        raise forms.ValidationError("An account with this email already exists.")
    return email


class AddEmailForm(forms.Form):
    """Asks a logged-in user with no email on file for one."""

    email = forms.EmailField(
        label="Email",
        help_text="Used to reset your password and to contact prize winners.",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_email(self):
        return unique_email(self.cleaned_data["email"], exclude_user=self.user)


class PredictionForm(forms.Form):
    choice = forms.ChoiceField(
        choices=Prediction.Side.choices,
        widget=forms.RadioSelect,
        label="Your pick",
    )

    def __init__(self, *args, match: Match, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            (Prediction.Side.A, str(match.team_a)),
            (Prediction.Side.B, str(match.team_b)),
        ]
        if match.allows_draw:
            choices.append((Prediction.Side.DRAW, "Draw"))
        self.fields["choice"].choices = choices
