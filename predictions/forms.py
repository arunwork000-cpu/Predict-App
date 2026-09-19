from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .locations import COUNTRY_CHOICES, STATE_CHOICES, states_for
from .models import Match, Prediction


class RegistrationForm(UserCreationForm):
    """UserCreationForm plus required Country/State dropdowns.

    Both fields are ChoiceFields (rendered as <select>), so a submission can
    only carry one of the values we listed -- never free text. The state
    list is deliberately every state across every supported country (the
    dependent-dropdown filtering is a client-side convenience); the actual
    country/state match is enforced in clean() below.
    """

    country = forms.ChoiceField(
        choices=[("", "Select a country")] + COUNTRY_CHOICES,
        label="Country",
    )
    state = forms.ChoiceField(
        choices=[("", "Select a country first")] + STATE_CHOICES,
        label="State / Province / Region",
    )

    class Meta(UserCreationForm.Meta):
        model = User

    def clean(self):
        cleaned_data = super().clean()
        country = cleaned_data.get("country")
        state = cleaned_data.get("state")
        if country and state and state not in states_for(country):
            self.add_error(
                "state", "Select a state that belongs to the chosen country."
            )
        return cleaned_data


class PredictionForm(forms.Form):
    choice = forms.ChoiceField(
        choices=Prediction.Side.choices,
        widget=forms.RadioSelect,
        label="Your pick",
    )

    def __init__(self, *args, match: Match, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["choice"].choices = [
            (Prediction.Side.A, str(match.team_a)),
            (Prediction.Side.B, str(match.team_b)),
        ]
