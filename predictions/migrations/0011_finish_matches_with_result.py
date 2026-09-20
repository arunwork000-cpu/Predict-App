from django.db import migrations
from django.db.models import Q


def finish_matches_with_result(apps, schema_editor):
    """Matches that already have a winner or draw are over: mark them Finished."""
    Match = apps.get_model("predictions", "Match")
    Match.objects.filter(status__in=["scheduled", "live"]).filter(
        Q(winner__isnull=False) | Q(is_draw=True)
    ).update(status="finished")


class Migration(migrations.Migration):

    dependencies = [
        ("predictions", "0010_match_draw"),
    ]

    operations = [
        migrations.RunPython(finish_matches_with_result, migrations.RunPython.noop),
    ]
