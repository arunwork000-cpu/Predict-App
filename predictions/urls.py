from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # Generic homepage: shows the default sport (Football).
    path("", views.home, name="match_list"),
    path("sport/<slug:sport_slug>/", views.sport_matches, name="sport_matches"),
    path("closed/", views.closed_matches_view, name="closed_matches"),
    path("matches/<int:pk>/", views.match_detail, name="match_detail"),
    path("matches/<int:pk>/predict/", views.predict, name="predict"),
    path("predictions/mine/", views.my_predictions, name="my_predictions"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    # Old separate pages now live on the combined page; keep the URLs alive.
    path(
        "leaderboard/monthly/",
        RedirectView.as_view(pattern_name="leaderboard", permanent=True),
        name="leaderboard_monthly",
    ),
    # Django has no built-in register view; login/logout are in config/urls.py.
    path("accounts/register/", views.register, name="register"),
    path("accounts/add-email/", views.add_email, name="add_email"),
]
