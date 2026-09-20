import zoneinfo

from django.utils import timezone


class TimezoneMiddleware:
    """Render datetimes in the visitor's own time zone.

    The browser reports its IANA zone name in a ``tz`` cookie (see the
    script in base.html). The value is validated before use; anything
    missing or unknown falls back to settings.TIME_ZONE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = request.COOKIES.get("tz")
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
