import zoneinfo
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone


class EmailRequiredMiddleware:
    """Send logged-in users who have no email on file to the "add email" page.

    Email used to be optional at signup, so older accounts have none, which
    means they could never get a password-reset link. Until they add one they
    can only reach the add-email page, log out, the admin, and static/media
    files.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not user.email and not self._exempt(request):
            url = reverse("add_email")
            if request.method == "GET":
                url += "?" + urlencode({"next": request.get_full_path()})
            return redirect(url)
        return self.get_response(request)

    @staticmethod
    def _exempt(request):
        path = request.path_info
        if path in (reverse("add_email"), reverse("logout")):
            return True
        prefixes = (
            reverse("admin:index"),
            settings.STATIC_URL,
            settings.MEDIA_URL,
        )
        return path.startswith(prefixes)


class TimezoneMiddleware:
    """Render datetimes in the visitor's own time zone.

    The browser reports its IANA zone name in a ``tz`` cookie (see the
    script in base.html). The value is validated before use; anything
    missing or unknown falls back to settings.TIME_ZONE (India Standard
    Time). The admin always uses settings.TIME_ZONE, so match times are
    entered and shown in IST no matter where the admin's browser is.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = request.COOKIES.get("tz")
        if request.path_info.startswith(reverse("admin:index")):
            name = None
        try:
            tz = zoneinfo.ZoneInfo(name) if name else None
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            tz = None

        if tz is not None:
            timezone.activate(tz)
        else:
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
