from django.contrib import admin, messages

from .models import Match, Prediction, Profile, Sport, Team
from .services import score_match


@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "sport")
    list_filter = ("sport",)
    search_fields = ("name",)
    autocomplete_fields = ("sport",)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "points")
    search_fields = ("user__username",)
    readonly_fields = ("user", "points")


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = (
        "team_a",
        "team_b",
        "sport",
        "start_time",
        "prediction_deadline",
        "status",
        "winner",
        "is_published",
        "is_scored",
    )
    list_filter = ("sport", "status", "is_published", "is_scored")
    search_fields = ("team_a__name", "team_b__name")
    autocomplete_fields = ("sport", "team_a", "team_b", "winner")

    def get_readonly_fields(self, request, obj=None):
        readonly = ["is_scored"]
        if obj and obj.is_scored:
            readonly.append("winner")
        return readonly

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.winner_id and not obj.is_scored:
            if score_match(obj.pk):
                messages.success(
                    request,
                    "Match scored. Correct predictions +10, incorrect -5.",
                )


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_display = ("user", "match", "choice", "updated_at")
    list_filter = ("choice",)
    search_fields = ("user__username", "match__team_a__name", "match__team_b__name")
    readonly_fields = ("user", "match", "choice", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
