from django.urls import path

from . import views

urlpatterns = [
    # Generic homepage: shows the default sport (Football).
    path("", views.home, name="match_list"),
    path("sport/<slug:sport_slug>/", views.sport_matches, name="sport_matches"),
    path("closed/", views.closed_matches_view, name="closed_matches"),
    path("matches/<int:pk>/", views.match_detail, name="match_detail"),
    path("matches/<int:pk>/predict/", views.predict, name="predict"),
    path("predictions/mine/", views.my_predictions, name="my_predictions"),
    path("leaderboard/", views.leaderboard_all_time, name="leaderboard_all_time"),
    path("leaderboard/monthly/", views.leaderboard_monthly, name="leaderboard_monthly"),
    # Django has no built-in register view; login/logout are in config/urls.py.
    path("accounts/register/", views.register, name="register"),
]
