from django import forms

from .models import Match, Prediction


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
