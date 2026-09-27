"""Sport names for the public navigation.

Migrations 0007_create_default_sports and 0009_create_hockey_sport seed these
as Sport rows. Kept separate from those migrations (which hardcode their own
copies) so future edits here can't silently change already-applied migration
history.
"""

SUPPORTED_SPORTS = ["Football", "Cricket", "Tennis", "Badminton", "Hockey"]
SPORT_SLUGS = {name.lower(): name for name in SUPPORTED_SPORTS}
DEFAULT_SPORT_SLUG = "football"

# Sports whose matches can end in a draw; only these offer a "Draw" pick.
DRAW_SPORTS = frozenset({"Football", "Cricket", "Hockey"})
