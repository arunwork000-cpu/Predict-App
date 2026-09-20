"""
URL configuration for config project.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from predictions.views import media_file

urlpatterns = [
    path("admin/", admin.site.urls),
    # Login uses Django's LoginView. Template: templates/registration/login.html
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(redirect_authenticated_user=True),
        name="login",
    ),
    # Built-in logout (POST only) plus password-reset URLs.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("predictions.urls")),
]

# Uploaded media (e.g. team flags) is stored in the database and served from
# there in every environment; see predictions.storage.DatabaseStorage.
urlpatterns += [
    path(f"{settings.MEDIA_URL.strip('/')}/<path:path>", media_file, name="media"),
]
