"""Date formats used everywhere Django localises dates (notably the admin).

Django's built-in ``en`` locale formats override the DATE_INPUT_FORMATS
setting, so the site-wide format lives here instead, loaded through
settings.FORMAT_MODULE_PATH. ISO input (2026-09-20) is still accepted: Django
appends the ISO formats to every input-format list automatically.
"""

# The first entry is what date widgets display and what the admin calendar
# popup writes back, e.g. 20-Sep-2026.
DATE_INPUT_FORMATS = ["%d-%b-%Y"]

DATE_FORMAT = "d-M-Y"
DATETIME_FORMAT = "d-M-Y, H:i"
