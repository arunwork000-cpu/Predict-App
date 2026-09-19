from django.db import migrations


def create_hockey_sport(apps, schema_editor):
    Sport = apps.get_model("predictions", "Sport")
    Sport.objects.get_or_create(name="Hockey")


class Migration(migrations.Migration):

    dependencies = [
        ("predictions", "0008_profile_country_profile_state_scoreadjustment"),
    ]

    operations = [
        migrations.RunPython(create_hockey_sport, migrations.RunPython.noop),
    ]
