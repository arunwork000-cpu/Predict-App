import zoneinfo

from django.urls import reverse
from django.utils import timezone


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
