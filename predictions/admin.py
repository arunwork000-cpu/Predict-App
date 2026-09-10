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
    date_hierarchy = "start_time"
    actions = ("publish_matches", "unpublish_matches")
    # is_scored is managed by the scoring service. winner stays editable even
    # after scoring so a mistaken result can be corrected (score_match then
    # reconciles the points).
    readonly_fields = ("is_scored",)

    @admin.action(description="Publish selected matches")
    def publish_matches(self, request, queryset):
        updated = queryset.update(is_published=True)
        self.message_user(request, f"{updated} match(es) published.", messages.SUCCESS)

    @admin.action(description="Unpublish selected matches")
    def unpublish_matches(self, request, queryset):
        updated = queryset.update(is_published=False)
        self.message_user(request, f"{updated} match(es) unpublished.", messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # score_match reconciles in every direction (first scoring, winner
        # correction, winner cleared) and safely no-ops for an unscored match
        # with no winner, so it is called on every save.
        if score_match(obj.pk):
            messages.success(
                request,
                "Predictions scored. Correct +10, incorrect -5.",
            )


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_display = ("user", "match", "choice", "points_awarded", "updated_at")
    list_filter = ("choice",)
    search_fields = ("user__username", "match__team_a__name", "match__team_b__name")
    readonly_fields = (
        "user",
        "match",
        "choice",
        "points_awarded",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
