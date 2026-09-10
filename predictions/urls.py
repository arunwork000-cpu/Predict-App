from django.urls import path

from . import views

urlpatterns = [
    path("", views.match_list, name="match_list"),
    path("matches/<int:pk>/predict/", views.predict, name="predict"),
    path("predictions/mine/", views.my_predictions, name="my_predictions"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    # Django has no built-in register view; login/logout are in config/urls.py.
    path("accounts/register/", views.register, name="register"),
]
