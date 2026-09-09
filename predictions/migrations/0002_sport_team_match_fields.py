import datetime

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predictions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Sport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=80, unique=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Team",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "sport",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="teams",
                        to="predictions.sport",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("sport", "name"),
                        name="unique_team_name_per_sport",
                    )
                ],
            },
        ),
        migrations.RemoveField(
            model_name="match",
            name="deadline",
        ),
        migrations.RemoveField(
            model_name="match",
            name="starts_at",
        ),
        migrations.RemoveField(
            model_name="match",
            name="team_a",
        ),
        migrations.RemoveField(
            model_name="match",
            name="team_b",
        ),
        migrations.RemoveField(
            model_name="match",
            name="winner",
        ),
        migrations.AddField(
            model_name="match",
            name="is_published",
            field=models.BooleanField(
                default=False,
                help_text="Unpublished matches stay hidden from the public list.",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="prediction_deadline",
            field=models.DateTimeField(
                default=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                help_text="Last moment a prediction is allowed. Usually the same as kickoff.",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="match",
            name="start_time",
            field=models.DateTimeField(
                default=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                help_text="When the match starts (kickoff).",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="match",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled"),
                    ("live", "Live"),
                    ("finished", "Finished"),
                    ("cancelled", "Cancelled"),
                ],
                default="scheduled",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="sport",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="matches",
                to="predictions.sport",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="team_a",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="home_matches",
                to="predictions.team",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="team_b",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="away_matches",
                to="predictions.team",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="winner",
            field=models.ForeignKey(
                blank=True,
                help_text="Leave empty until the match is over. Must be Team A or Team B.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="won_matches",
                to="predictions.team",
            ),
        ),
        migrations.AlterModelOptions(
            name="match",
            options={"ordering": ["start_time"]},
        ),
        migrations.AddConstraint(
            model_name="match",
            constraint=models.CheckConstraint(
                condition=~models.Q(team_a=models.F("team_b")),
                name="match_team_a_ne_team_b",
            ),
        ),
    ]
