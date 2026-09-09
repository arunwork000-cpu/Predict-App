from django.apps import AppConfig


class PredictionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "predictions"

    def ready(self):
        # Connect the "create Profile when a User is created" signal.
        from . import signals  # noqa: F401
