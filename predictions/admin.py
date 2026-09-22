import json

from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

from .models import (
    CreditLedger,
    Match,
    Prediction,
    Profile,
    Referral,
    ReferralSettings,
    ScoreAdjustment,
    Sport,
    Team,
    VoucherRedemption,
)
from .services import (
    fulfill_redemption,
    reject_redemption,
    score_match,
    sync_match_statuses,
)


@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "sport", "flag_preview")
    list_filter = ("sport",)
    search_fields = ("name",)
    autocomplete_fields = ("sport",)
    readonly_fields = ("flag_preview",)
    fields = ("name", "sport", "flag", "flag_preview")

    @admin.display(description="Flag")
    def flag_preview(self, obj):
        if not obj.flag:
            return "(no flag uploaded)"
        return format_html(
            '<img src="{}" alt="{} flag" style="height:24px;width:auto;">',
            obj.flag.url,
            obj.name,
        )


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    # country/state stay editable (not in readonly_fields) so an admin can
    # correct a legacy account that has none, or fix a typo. points/credits
    # and referral_code are system-managed -- see services.py.
    list_display = ("user", "points", "credits", "referral_code", "age", "state", "country")
    list_filter = ("country",)
    search_fields = ("user__username", "state", "country", "referral_code")
    readonly_fields = ("user", "points", "credits", "referral_code")
    fields = ("user", "points", "credits", "referral_code", "age", "country", "state")


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MatchAdminForm(forms.ModelForm):
    """Match form whose team dropdowns follow the chosen sport.

    Team A / Team B list only teams of the selected sport, and Winner lists
    only the two chosen teams. The browser refills them when the sport
    changes (static/predictions/admin/match_teams.js); the querysets here
    make the server enforce the same rule.
    """

    class Meta:
        model = Match
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance

        if self.is_bound:
            sport_id = _int_or_none(self.data.get("sport"))
            team_ids = [
                _int_or_none(self.data.get("team_a")),
                _int_or_none(self.data.get("team_b")),
            ]
        else:
            sport_id = instance.sport_id or _int_or_none(self.initial.get("sport"))
            team_ids = [instance.team_a_id, instance.team_b_id]

        teams = (
            Team.objects.filter(sport_id=sport_id)
            if sport_id
            else Team.objects.none()
        )
        self.fields["team_a"].queryset = teams
        self.fields["team_b"].queryset = teams
        empty = "---------" if sport_id else "Select a sport first"
        self.fields["team_a"].empty_label = empty
        self.fields["team_b"].empty_label = empty
        self.fields["winner"].queryset = Team.objects.filter(
            pk__in=[t for t in team_ids if t]
        )

        # sport -> [[team id, team name], ...] for the dropdown script.
        by_sport = {}
        for team_id, name, team_sport_id in Team.objects.values_list(
            "id", "name", "sport_id"
        ):
            by_sport.setdefault(team_sport_id, []).append([team_id, name])
        # The admin wraps FK widgets (add/change links); the <select> itself is
        # the inner widget.
        sport_widget = self.fields["sport"].widget
        sport_widget = getattr(sport_widget, "widget", sport_widget)
        sport_widget.attrs["data-teams"] = json.dumps(by_sport)


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    form = MatchAdminForm

    class Media:
        js = (
            "predictions/admin/match_teams.js",
            "predictions/admin/match_deadline.js",
            "predictions/admin/match_tomorrow.js",
        )

    list_display = (
        "team_a",
        "team_b",
        "sport",
        "event_name",
        "status",
        "start_time",
        "prediction_deadline",
        "winner",
        "is_draw",
        "is_published",
        "is_scored",
    )
    list_filter = ("sport", "status", "is_draw", "is_published", "is_scored")
    search_fields = ("team_a__name", "team_b__name", "event_name")
    # sport/team_a/team_b/winner are plain dropdowns (not autocomplete) so
    # they can be filtered by sport; see MatchAdminForm.
    date_hierarchy = "start_time"
    actions = ("publish_matches", "unpublish_matches")
    # is_scored is managed by the scoring service. winner stays editable even
    # after scoring so a mistaken result can be corrected (score_match then
    # reconciles the points).
    readonly_fields = ("is_scored",)
    fieldsets = (
        (None, {
            "fields": (
                "sport",
                "event_name",
                "team_a",
                "team_b",
                "start_time",
                "prediction_deadline",
                "status",
                "winner",
                "is_draw",
                "is_published",
                "is_scored",
            ),
        }),
        ("Points", {
            "fields": (
                "team_a_win_points",
                "team_a_lose_points",
                "team_b_win_points",
                "team_b_lose_points",
                "draw_win_points",
                "draw_lose_points",
            ),
            "description": (
                "Points awarded for a correct/incorrect pick. Defaults: win 10, lose -5. "
                "The Draw points apply only to Football, Cricket and Hockey. "
                "Enter 0 in both Draw fields if the match cannot end in a draw: "
                "the Draw box is then hidden and users pick only Team A or Team B."
            ),
        }),
    )

    def get_queryset(self, request):
        # Past-deadline Scheduled matches show as Awaiting result here too.
        sync_match_statuses()
        return super().get_queryset(request)

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
                "Predictions scored using this match's configured points.",
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


@admin.register(ScoreAdjustment)
class ScoreAdjustmentAdmin(admin.ModelAdmin):
    """Read-only audit trail backing the monthly leaderboard."""

    list_display = ("user", "match", "delta", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "match__team_a__name", "match__team_b__name")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReferralSettings)
class ReferralSettingsAdmin(admin.ModelAdmin):
    """Singleton: credits per referral and the redemption threshold."""

    fields = ("credits_per_referral", "redemption_threshold")

    def has_add_permission(self, request):
        return not ReferralSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    """Read-only audit trail; status is system-managed (see services.py)."""

    list_display = ("referrer", "referred_user", "status", "created_at", "credited_at")
    list_filter = ("status",)
    search_fields = ("referrer__username", "referred_user__username")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(VoucherRedemption)
class VoucherRedemptionAdmin(admin.ModelAdmin):
    """Manual fulfillment workflow: an admin arranges the actual voucher
    outside this system, then runs one of the actions below. `status` stays
    read-only on the change form so it can only change through those
    actions, never a stray manual edit."""

    list_display = ("user", "credits_spent", "status", "requested_at", "resolved_at")
    list_filter = ("status",)
    search_fields = ("user__username",)
    readonly_fields = ("user", "credits_spent", "status", "requested_at", "resolved_at")
    fields = ("user", "credits_spent", "status", "admin_note", "requested_at", "resolved_at")
    actions = ("mark_fulfilled", "reject_and_refund")

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark selected as fulfilled")
    def mark_fulfilled(self, request, queryset):
        updated = sum(fulfill_redemption(r.pk) for r in queryset)
        self.message_user(request, f"{updated} redemption(s) marked fulfilled.", messages.SUCCESS)

    @admin.action(description="Reject selected and refund credits")
    def reject_and_refund(self, request, queryset):
        updated = sum(reject_redemption(r.pk) for r in queryset)
        self.message_user(request, f"{updated} redemption(s) rejected and refunded.", messages.SUCCESS)


@admin.register(CreditLedger)
class CreditLedgerAdmin(admin.ModelAdmin):
    """Read-only audit trail, same convention as ScoreAdjustmentAdmin."""

    list_display = ("user", "delta", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("user__username",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
