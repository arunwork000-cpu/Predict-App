from django.db import migrations

SUPPORTED_SPORTS = ["Football", "Cricket", "Tennis", "Badminton"]


def create_default_sports(apps, schema_editor):
    Sport = apps.get_model("predictions", "Sport")
    for name in SUPPORTED_SPORTS:
        Sport.objects.get_or_create(name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("predictions", "0006_match_event_name"),
    ]

    operations = [
        migrations.RunPython(create_default_sports, migrations.RunPython.noop),
    ]
