"""
URL configuration for config project.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

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

if settings.DEBUG:
    # Serve uploaded media (e.g. team flags) locally. In production this is
    # handled by the web server / storage backend, not Django.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
