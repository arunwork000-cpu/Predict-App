import os
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import MatchAdmin
from .models import Match, Prediction, Profile, Sport, Team
from .services import POINTS_CORRECT, POINTS_WRONG, score_match


def future_match(**kwargs):
    now = timezone.now()
    sport = kwargs.pop("sport", None) or Sport.objects.create(name="Football")
    team_a = kwargs.pop("team_a", None) or Team.objects.create(
        name="Lions", sport=sport
    )
    team_b = kwargs.pop("team_b", None) or Team.objects.create(
        name="Tigers", sport=sport
    )
    defaults = {
        "sport": sport,
        "team_a": team_a,
        "team_b": team_b,
        "start_time": now + timedelta(hours=2),
        "prediction_deadline": now + timedelta(hours=2),
        "status": Match.Status.SCHEDULED,
        "is_published": True,
    }
    defaults.update(kwargs)
    return Match.objects.create(**defaults)


class ProfileSignalTests(TestCase):
    def test_profile_created_with_user(self):
        user = User.objects.create_user("alice", password="pass12345")
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.points, 0)


class ScoringTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pass12345")
        self.bob = User.objects.create_user("bob", password="pass12345")
        self.match = future_match()
        # alice picks Team A, bob picks Team B.
        Prediction.objects.create(user=self.alice, match=self.match, choice="A")
        Prediction.objects.create(user=self.bob, match=self.match, choice="B")

    def _set_winner(self, side):
        self.match.refresh_from_db()
        self.match.winner = (
            self.match.team_a if side == "A" else self.match.team_b
        )
        self.match.save()

    def _clear_winner(self):
        self.match.refresh_from_db()
        self.match.winner = None
        self.match.save()

    def _pred(self, user):
        return Prediction.objects.get(user=user, match=self.match)

    def _points(self, user):
        user.profile.refresh_from_db()
        return user.profile.points

    # --- existing behaviour, still intact -------------------------------

    def test_correct_and_incorrect_points(self):
        self._set_winner("A")
        self.assertTrue(score_match(self.match.pk))
        self.match.refresh_from_db()
        self.assertEqual(self._points(self.alice), POINTS_CORRECT)
        self.assertEqual(self._points(self.bob), POINTS_WRONG)
        self.assertTrue(self.match.is_scored)

    def test_scoring_is_idempotent(self):
        self._set_winner("A")
        self.assertTrue(score_match(self.match.pk))
        self.assertFalse(score_match(self.match.pk))
        self.assertEqual(self._points(self.alice), POINTS_CORRECT)

    def test_score_without_winner_does_nothing(self):
        self.assertFalse(score_match(self.match.pk))
        self.assertEqual(self._points(self.alice), 0)

    def test_user_without_a_prediction_is_not_affected(self):
        carol = User.objects.create_user("carol", password="pass12345")
        self._set_winner("A")
        score_match(self.match.pk)
        self.assertEqual(self._points(carol), 0)

    # --- points stored per prediction ---------------------------------

    def test_unscored_prediction_has_no_points_awarded(self):
        pred = self._pred(self.alice)
        self.assertIsNone(pred.points_awarded)
        self.assertIsNone(pred.points_earned)

    def test_points_awarded_is_stored_after_initial_scoring(self):
        self._set_winner("A")
        score_match(self.match.pk)
        # alice picked the winner, bob did not.
        self.assertEqual(self._pred(self.alice).points_awarded, POINTS_CORRECT)
        self.assertEqual(self._pred(self.bob).points_awarded, POINTS_WRONG)
        self.assertEqual(self._pred(self.alice).points_earned, POINTS_CORRECT)
        self.assertEqual(self._pred(self.bob).points_earned, POINTS_WRONG)

    def test_rescoring_same_winner_makes_no_further_change(self):
        self._set_winner("A")
        self.assertTrue(score_match(self.match.pk))
        points_after_first = self._points(self.alice)
        awarded_after_first = self._pred(self.alice).points_awarded

        self.assertFalse(score_match(self.match.pk))
        self.assertFalse(score_match(self.match.pk))

        self.assertEqual(self._points(self.alice), points_after_first)
        self.assertEqual(
            self._pred(self.alice).points_awarded, awarded_after_first
        )

    # --- safe result correction --------------------------------------

    def test_correcting_winner_reconciles_points_awarded(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._set_winner("B")
        self.assertTrue(score_match(self.match.pk))
        # A now loses, B now wins.
        self.assertEqual(self._pred(self.alice).points_awarded, POINTS_WRONG)
        self.assertEqual(self._pred(self.bob).points_awarded, POINTS_CORRECT)

    def test_correcting_winner_reconciles_profile_points_without_double_counting(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self.assertEqual(self._points(self.alice), POINTS_CORRECT)   # +10
        self.assertEqual(self._points(self.bob), POINTS_WRONG)       # -5

        self._set_winner("B")
        score_match(self.match.pk)
        # alice: +10 -> -5 (delta -15); bob: -5 -> +10 (delta +15)
        self.assertEqual(self._points(self.alice), POINTS_WRONG)
        self.assertEqual(self._points(self.bob), POINTS_CORRECT)

    def test_rescoring_after_correction_is_also_idempotent(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._set_winner("B")
        self.assertTrue(score_match(self.match.pk))
        self.assertFalse(score_match(self.match.pk))
        self.assertEqual(self._points(self.alice), POINTS_WRONG)
        self.assertEqual(self._points(self.bob), POINTS_CORRECT)

    # --- clearing the winner unwinds a scored match -------------------

    def test_clearing_winner_reverses_profile_points(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self.assertEqual(self._points(self.alice), POINTS_CORRECT)
        self.assertEqual(self._points(self.bob), POINTS_WRONG)

        self._clear_winner()
        self.assertTrue(score_match(self.match.pk))
        self.assertEqual(self._points(self.alice), 0)
        self.assertEqual(self._points(self.bob), 0)

    def test_clearing_winner_resets_points_awarded_to_none(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._clear_winner()
        score_match(self.match.pk)
        self.assertIsNone(self._pred(self.alice).points_awarded)
        self.assertIsNone(self._pred(self.bob).points_awarded)

    def test_clearing_winner_resets_is_scored_to_false(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._clear_winner()
        score_match(self.match.pk)
        self.match.refresh_from_db()
        self.assertFalse(self.match.is_scored)

    def test_clearing_winner_again_is_idempotent(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._clear_winner()
        self.assertTrue(score_match(self.match.pk))
        self.assertFalse(score_match(self.match.pk))
        self.assertEqual(self._points(self.alice), 0)
        self.assertEqual(self._points(self.bob), 0)

    def test_scoring_after_clearing_winner_scores_normally(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._clear_winner()
        score_match(self.match.pk)

        self._set_winner("B")
        self.assertTrue(score_match(self.match.pk))
        self.match.refresh_from_db()
        self.assertTrue(self.match.is_scored)
        # alice picked A, bob picked B; B now wins.
        self.assertEqual(self._points(self.alice), POINTS_WRONG)
        self.assertEqual(self._points(self.bob), POINTS_CORRECT)
        self.assertEqual(self._pred(self.alice).points_awarded, POINTS_WRONG)
        self.assertEqual(self._pred(self.bob).points_awarded, POINTS_CORRECT)


class MatchAdminScoringTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.match_admin = MatchAdmin(Match, AdminSite())
        self.staff = User.objects.create_user(
            "staff", password="pass12345", is_staff=True, is_superuser=True
        )
        self.alice = User.objects.create_user("alice", password="pass12345")
        self.bob = User.objects.create_user("bob", password="pass12345")
        self.match = future_match()
        Prediction.objects.create(user=self.alice, match=self.match, choice="A")
        Prediction.objects.create(user=self.bob, match=self.match, choice="B")

    def _admin_set_winner(self, side):
        # readonly is_scored keeps its DB value through an admin save, so mirror
        # that by refreshing before editing.
        self.match.refresh_from_db()
        self.match.winner = (
            self.match.team_a if side == "A" else self.match.team_b
        )
        request = self.factory.post("/admin/predictions/match/")
        request.user = self.staff
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        self.match_admin.save_model(request, self.match, form=None, change=True)

    def _admin_clear_winner(self):
        self.match.refresh_from_db()
        self.match.winner = None
        request = self.factory.post("/admin/predictions/match/")
        request.user = self.staff
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        self.match_admin.save_model(request, self.match, form=None, change=True)

    def _points(self, user):
        user.profile.refresh_from_db()
        return user.profile.points

    def test_setting_winner_in_admin_scores_predictions(self):
        self._admin_set_winner("A")
        self.match.refresh_from_db()
        self.assertTrue(self.match.is_scored)
        self.assertEqual(self._points(self.alice), POINTS_CORRECT)
        self.assertEqual(self._points(self.bob), POINTS_WRONG)
        pred = Prediction.objects.get(user=self.alice, match=self.match)
        self.assertEqual(pred.points_awarded, POINTS_CORRECT)

    def test_correcting_winner_in_admin_reconciles_the_score(self):
        self._admin_set_winner("A")
        self._admin_set_winner("B")
        self.assertEqual(self._points(self.alice), POINTS_WRONG)
        self.assertEqual(self._points(self.bob), POINTS_CORRECT)
        self.assertEqual(
            Prediction.objects.get(user=self.alice, match=self.match).points_awarded,
            POINTS_WRONG,
        )
        self.assertEqual(
            Prediction.objects.get(user=self.bob, match=self.match).points_awarded,
            POINTS_CORRECT,
        )

    def test_clearing_winner_in_admin_unwinds_the_score(self):
        self._admin_set_winner("A")
        self._admin_clear_winner()

        self.assertEqual(self._points(self.alice), 0)
        self.assertEqual(self._points(self.bob), 0)
        self.assertIsNone(
            Prediction.objects.get(user=self.alice, match=self.match).points_awarded
        )
        self.assertIsNone(
            Prediction.objects.get(user=self.bob, match=self.match).points_awarded
        )
        self.match.refresh_from_db()
        self.assertFalse(self.match.is_scored)


class UniquePredictionTests(TestCase):
    def test_one_prediction_per_user_match(self):
        user = User.objects.create_user("alice", password="pass12345")
        match = future_match()
        Prediction.objects.create(user=user, match=match, choice="A")
        with self.assertRaises(IntegrityError):
            Prediction.objects.create(user=user, match=match, choice="B")


class PredictViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pass12345")
        self.client.login(username="alice", password="pass12345")

    def test_can_predict_before_deadline(self):
        match = future_match()
        url = reverse("predict", args=[match.pk])
        response = self.client.post(url, {"choice": "A"})
        self.assertRedirects(response, reverse("match_list"))
        pick = Prediction.objects.get(user=self.user, match=match)
        self.assertEqual(pick.choice, "A")

    def test_can_change_pick_before_deadline(self):
        match = future_match()
        url = reverse("predict", args=[match.pk])
        self.client.post(url, {"choice": "A"})
        self.client.post(url, {"choice": "B"})
        pick = Prediction.objects.get(user=self.user, match=match)
        self.assertEqual(pick.choice, "B")
        self.assertEqual(Prediction.objects.filter(user=self.user, match=match).count(), 1)

    def test_late_prediction_rejected(self):
        match = future_match(
            start_time=timezone.now() - timedelta(hours=1),
            prediction_deadline=timezone.now() - timedelta(minutes=1),
        )
        url = reverse("predict", args=[match.pk])
        response = self.client.post(url, {"choice": "A"})
        self.assertRedirects(response, reverse("match_list"))
        self.assertFalse(Prediction.objects.filter(user=self.user, match=match).exists())

    def test_unauthenticated_cannot_predict(self):
        self.client.logout()
        match = future_match()
        url = reverse("predict", args=[match.pk])
        response = self.client.post(url, {"choice": "A"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.assertFalse(Prediction.objects.exists())

    def test_predict_page_renders_for_open_match(self):
        match = future_match()
        response = self.client.get(reverse("predict", args=[match.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "predictions/predict.html")
        self.assertContains(response, str(match.team_a))
        self.assertContains(response, str(match.team_b))

    def test_cancel_link_returns_to_match_detail(self):
        match = future_match()
        response = self.client.get(reverse("predict", args=[match.pk]))
        detail_url = reverse("match_detail", args=[match.pk])
        # Exact href match: the predict URL (.../predict/) cannot satisfy this.
        self.assertContains(response, 'href="%s"' % detail_url)


class AccountTests(TestCase):
    def test_register_creates_user_profile_and_logs_in(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )
        self.assertRedirects(response, reverse("match_list"))
        user = User.objects.get(username="newuser")
        self.assertTrue(Profile.objects.filter(user=user).exists())
        home = self.client.get(reverse("match_list"))
        self.assertContains(home, "newuser")
        self.assertContains(home, "Log out")
        self.assertNotContains(home, 'href="/accounts/login/"')

    def test_register_rejects_duplicate_username(self):
        User.objects.create_user("taken", password="StrongPass123")
        response = self.client.post(
            reverse("register"),
            {
                "username": "taken",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="taken").count(), 1)

    def test_login_and_logout(self):
        User.objects.create_user("alice", password="StrongPass123")
        guest = self.client.get(reverse("match_list"))
        self.assertContains(guest, "Log in")
        self.assertContains(guest, "Register")

        bad = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "wrong-password"},
        )
        self.assertEqual(bad.status_code, 200)

        ok = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "StrongPass123"},
        )
        self.assertRedirects(ok, reverse("match_list"))
        logged_in = self.client.get(reverse("match_list"))
        self.assertContains(logged_in, "alice")
        self.assertContains(logged_in, "Log out")

        logged_out = self.client.post(reverse("logout"))
        self.assertRedirects(logged_out, reverse("match_list"))
        guest_again = self.client.get(reverse("match_list"))
        self.assertContains(guest_again, "Log in")

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Log in")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_register_page_renders_navbar_for_guests(self):
        response = self.client.get(reverse("register"))
        self.assertContains(response, "Create an account")
        self.assertContains(response, "Log in")
        self.assertContains(response, "Register")
        self.assertNotContains(response, "Log out")

    def test_logged_in_user_is_redirected_away_from_register(self):
        User.objects.create_user("alice", password="StrongPass123")
        self.client.login(username="alice", password="StrongPass123")
        response = self.client.get(reverse("register"))
        self.assertRedirects(response, reverse("match_list"))

    def test_logout_get_is_not_allowed(self):
        User.objects.create_user("alice", password="StrongPass123")
        self.client.login(username="alice", password="StrongPass123")
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)


class PasswordResetFlowTests(TestCase):
    NEW_PASSWORD = "StrongNewPass123"

    def setUp(self):
        self.user = User.objects.create_user(
            "resetuser", email="reset@example.com", password="oldpass12345"
        )

    def _request_reset(self, email="reset@example.com"):
        return self.client.post(reverse("password_reset"), {"email": email})

    def test_password_reset_page_renders_on_project_shell(self):
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/password_reset_form.html")
        self.assertTemplateUsed(response, "base.html")
        self.assertContains(response, "Sports Predictions")  # navbar brand

    def test_login_page_links_to_password_reset(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("password_reset"))
        self.assertContains(response, "Forgot your password?")

    def test_valid_email_redirects_to_done_and_sends_one_email(self):
        response = self._request_reset()
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset@example.com", mail.outbox[0].to)

    def test_done_page_renders_on_project_shell(self):
        response = self.client.get(reverse("password_reset_done"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/password_reset_done.html")
        self.assertContains(response, "Sports Predictions")

    def test_full_flow_from_email_link_to_login_with_new_password(self):
        self._request_reset()
        link = re.search(
            r"/accounts/reset/[^/\s]+/[^/\s]+/", mail.outbox[0].body
        )
        self.assertIsNotNone(link, "reset email should contain a reset link")

        # The emailed link redirects to the set-password page.
        redirected = self.client.get(link.group(0))
        self.assertEqual(redirected.status_code, 302)
        set_password_url = redirected.url

        confirm_page = self.client.get(set_password_url)
        self.assertEqual(confirm_page.status_code, 200)
        self.assertTemplateUsed(
            confirm_page, "registration/password_reset_confirm.html"
        )
        self.assertContains(confirm_page, "Sports Predictions")

        completed = self.client.post(
            set_password_url,
            {
                "new_password1": self.NEW_PASSWORD,
                "new_password2": self.NEW_PASSWORD,
            },
        )
        self.assertRedirects(completed, reverse("password_reset_complete"))

        complete_page = self.client.get(reverse("password_reset_complete"))
        self.assertEqual(complete_page.status_code, 200)
        self.assertTemplateUsed(
            complete_page, "registration/password_reset_complete.html"
        )
        self.assertContains(complete_page, "Sports Predictions")

        self.assertTrue(
            self.client.login(username="resetuser", password=self.NEW_PASSWORD)
        )

    def test_invalid_reset_link_shows_error_state(self):
        url = reverse(
            "password_reset_confirm",
            kwargs={"uidb64": "MQ", "token": "set-password"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["validlink"])
        self.assertContains(response, "invalid")
        self.assertContains(response, reverse("password_reset"))

    def test_unknown_email_does_not_reveal_account_and_sends_nothing(self):
        response = self._request_reset(email="nobody@example.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)


class PasswordChangeFlowTests(TestCase):
    OLD_PASSWORD = "oldpass12345"
    NEW_PASSWORD = "StrongNewPass123"

    def setUp(self):
        self.user = User.objects.create_user(
            "changeuser", password=self.OLD_PASSWORD
        )

    def _login(self):
        self.client.login(username="changeuser", password=self.OLD_PASSWORD)

    def _change(self, old, new):
        return self.client.post(
            reverse("password_change"),
            {"old_password": old, "new_password1": new, "new_password2": new},
        )

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_authenticated_page_renders_on_project_shell(self):
        self._login()
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/password_change_form.html")
        self.assertTemplateUsed(response, "base.html")
        self.assertContains(response, "Sports Predictions")
        self.assertNotContains(response, 'id="content-main"')

    def test_navbar_exposes_change_password_link(self):
        self._login()
        response = self.client.get(reverse("match_list"))
        self.assertContains(response, reverse("password_change"))
        self.assertContains(response, "Change password")

    def test_wrong_old_password_is_rejected(self):
        self._login()
        response = self._change("not-the-old-password", self.NEW_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.OLD_PASSWORD))

    def test_successful_change_redirects_to_done_on_project_shell(self):
        self._login()
        response = self._change(self.OLD_PASSWORD, self.NEW_PASSWORD)
        self.assertRedirects(response, reverse("password_change_done"))

        done = self.client.get(reverse("password_change_done"))
        self.assertEqual(done.status_code, 200)
        self.assertTemplateUsed(done, "registration/password_change_done.html")
        self.assertTemplateUsed(done, "base.html")
        self.assertContains(done, "Sports Predictions")

    def test_successful_change_keeps_session_and_swaps_password(self):
        self._login()
        self._change(self.OLD_PASSWORD, self.NEW_PASSWORD)

        # Same client stays authenticated (update_session_auth_hash).
        still_in = self.client.get(reverse("my_predictions"))
        self.assertEqual(still_in.status_code, 200)

        # New password works for a fresh session; old password does not.
        self.assertTrue(
            Client().login(username="changeuser", password=self.NEW_PASSWORD)
        )
        self.assertFalse(
            Client().login(username="changeuser", password=self.OLD_PASSWORD)
        )


class PageTests(TestCase):
    def test_home_empty_state(self):
        response = self.client.get(reverse("match_list"))
        self.assertContains(response, "No upcoming matches")

    def test_leaderboard_empty_state(self):
        response = self.client.get(reverse("leaderboard"))
        self.assertContains(response, "No players yet")


class MatchModelTests(TestCase):
    def setUp(self):
        self.sport = Sport.objects.create(name="Cricket")
        self.team_a = Team.objects.create(name="India", sport=self.sport)
        self.team_b = Team.objects.create(name="Australia", sport=self.sport)
        self.now = timezone.now()

    def valid_kwargs(self, **overrides):
        data = {
            "sport": self.sport,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "start_time": self.now + timedelta(hours=2),
            "prediction_deadline": self.now + timedelta(hours=1),
            "status": Match.Status.SCHEDULED,
            "is_published": True,
        }
        data.update(overrides)
        return data

    def test_valid_match_saves(self):
        match = Match(**self.valid_kwargs())
        match.full_clean()
        match.save()
        self.assertEqual(Match.objects.count(), 1)

    def test_same_teams_fail_full_clean(self):
        match = Match(**self.valid_kwargs(team_b=self.team_a))
        with self.assertRaises(ValidationError) as ctx:
            match.full_clean()
        self.assertIn("team_b", ctx.exception.message_dict)

    def test_same_teams_rejected_by_database(self):
        with self.assertRaises(IntegrityError):
            Match.objects.create(**self.valid_kwargs(team_b=self.team_a))

    def test_winner_must_be_team_a_or_team_b(self):
        other_sport = Sport.objects.create(name="Tennis")
        outsider = Team.objects.create(name="Outsider", sport=other_sport)
        match = Match(**self.valid_kwargs(winner=outsider))
        with self.assertRaises(ValidationError) as ctx:
            match.full_clean()
        self.assertIn("winner", ctx.exception.message_dict)

    def test_winner_can_be_team_a(self):
        match = Match(**self.valid_kwargs(winner=self.team_a))
        match.full_clean()
        match.save()
        self.assertEqual(match.winner, self.team_a)

    def test_teams_must_belong_to_match_sport(self):
        hockey = Sport.objects.create(name="Hockey")
        blades = Team.objects.create(name="Blades", sport=hockey)
        match = Match(**self.valid_kwargs(team_b=blades))
        with self.assertRaises(ValidationError):
            match.full_clean()

    def test_deadline_cannot_be_after_kickoff(self):
        match = Match(
            **self.valid_kwargs(
                start_time=self.now + timedelta(hours=1),
                prediction_deadline=self.now + timedelta(hours=2),
            )
        )
        with self.assertRaises(ValidationError) as ctx:
            match.full_clean()
        self.assertIn("prediction_deadline", ctx.exception.message_dict)


class SportModelTests(TestCase):
    def test_str_is_name(self):
        self.assertEqual(str(Sport.objects.create(name="Football")), "Football")

    def test_name_must_be_unique_in_the_database(self):
        Sport.objects.create(name="Football")
        with self.assertRaises(IntegrityError):
            Sport.objects.create(name="Football")

    def test_duplicate_name_fails_full_clean(self):
        Sport.objects.create(name="Football")
        with self.assertRaises(ValidationError):
            Sport(name="Football").full_clean()


class TeamModelTests(TestCase):
    def setUp(self):
        self.football = Sport.objects.create(name="Football")
        self.cricket = Sport.objects.create(name="Cricket")

    def test_str_is_name(self):
        team = Team.objects.create(name="Lions", sport=self.football)
        self.assertEqual(str(team), "Lions")

    def test_name_must_be_unique_per_sport(self):
        Team.objects.create(name="Lions", sport=self.football)
        with self.assertRaises(IntegrityError):
            Team.objects.create(name="Lions", sport=self.football)

    def test_same_name_allowed_in_a_different_sport(self):
        Team.objects.create(name="Lions", sport=self.football)
        Team.objects.create(name="Lions", sport=self.cricket)
        self.assertEqual(Team.objects.filter(name="Lions").count(), 2)

    def test_duplicate_per_sport_fails_full_clean(self):
        Team.objects.create(name="Lions", sport=self.football)
        with self.assertRaises(ValidationError):
            Team(name="Lions", sport=self.football).full_clean()

    def test_sport_is_protected_while_teams_exist(self):
        Team.objects.create(name="Lions", sport=self.football)
        with self.assertRaises(ProtectedError):
            self.football.delete()


class MyPredictionsViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pass12345")
        self.bob = User.objects.create_user("bob", password="pass12345")
        self.url = reverse("my_predictions")

    def _match(self, label):
        sport = Sport.objects.create(name=f"Sport {label}")
        return future_match(
            sport=sport,
            team_a=Team.objects.create(name=f"A {label}", sport=sport),
            team_b=Team.objects.create(name=f"B {label}", sport=sport),
        )

    def _score(self, match, winning_side):
        match.winner = match.team_a if winning_side == "A" else match.team_b
        match.save()
        self.assertTrue(score_match(match.pk))
        match.refresh_from_db()

    def test_login_is_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_user_sees_only_their_own_predictions(self):
        mine = self._match("mine")
        theirs = self._match("theirs")
        Prediction.objects.create(user=self.alice, match=mine, choice="A")
        Prediction.objects.create(user=self.bob, match=theirs, choice="A")

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        shown = list(response.context["pending"]) + list(response.context["decided"])
        self.assertEqual([p.match_id for p in shown], [mine.id])
        self.assertContains(response, "A mine")
        self.assertNotContains(response, "A theirs")

    def test_correct_prediction_shows_hit_and_plus_ten(self):
        match = self._match("hit")
        Prediction.objects.create(user=self.alice, match=match, choice="A")
        self._score(match, "A")

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        decided = response.context["decided"]
        self.assertEqual(len(decided), 1)
        self.assertIs(decided[0].is_correct, True)
        self.assertEqual(decided[0].points_earned, POINTS_CORRECT)
        self.assertContains(response, "Hit")
        self.assertContains(response, "+10")

    def test_incorrect_prediction_shows_miss_and_minus_five(self):
        match = self._match("miss")
        Prediction.objects.create(user=self.alice, match=match, choice="A")
        self._score(match, "B")

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        decided = response.context["decided"]
        self.assertEqual(len(decided), 1)
        self.assertIs(decided[0].is_correct, False)
        self.assertEqual(decided[0].points_earned, POINTS_WRONG)
        self.assertContains(response, "Miss")
        self.assertContains(response, "-5")

    def test_pending_and_decided_are_separated(self):
        pending_match = self._match("pending")
        decided_match = self._match("decided")
        Prediction.objects.create(user=self.alice, match=pending_match, choice="A")
        Prediction.objects.create(user=self.alice, match=decided_match, choice="A")
        self._score(decided_match, "A")

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        self.assertEqual(
            [p.match_id for p in response.context["pending"]], [pending_match.id]
        )
        self.assertEqual(
            [p.match_id for p in response.context["decided"]], [decided_match.id]
        )

    def test_cancelled_match_prediction_is_shown_as_cancelled_not_pending(self):
        match = self._match("cancelled")
        Prediction.objects.create(user=self.alice, match=match, choice="A")
        match.status = Match.Status.CANCELLED
        match.save()

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        cancelled = list(response.context["cancelled"])
        self.assertEqual([p.match_id for p in cancelled], [match.id])
        self.assertNotIn(
            match.id, [p.match_id for p in response.context["pending"]]
        )
        self.assertNotIn(
            match.id, [p.match_id for p in response.context["decided"]]
        )

        prediction = cancelled[0]
        self.assertIsNone(prediction.is_correct)     # never a Hit or Miss
        self.assertIsNone(prediction.points_earned)  # no earned scoring result

        self.assertContains(response, "Cancelled")
        self.assertContains(response, "<td>0</td>")
        self.assertNotContains(response, "Hit")
        self.assertNotContains(response, "Miss")

    def test_live_match_prediction_shows_live_badge_and_stays_pending(self):
        match = self._match("live")
        Prediction.objects.create(user=self.alice, match=match, choice="A")
        match.status = Match.Status.LIVE
        match.save()

        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)

        pending = list(response.context["pending"])
        self.assertEqual([p.match_id for p in pending], [match.id])
        self.assertNotIn(
            match.id, [p.match_id for p in response.context["decided"]]
        )
        self.assertNotIn(
            match.id, [p.match_id for p in response.context["cancelled"]]
        )

        prediction = pending[0]
        self.assertIsNone(prediction.is_correct)
        self.assertIsNone(prediction.points_earned)

        self.assertContains(response, "Live")
        self.assertNotContains(response, "Hit")
        self.assertNotContains(response, "Miss")

    def test_empty_state_for_user_with_no_predictions(self):
        self.client.login(username="alice", password="pass12345")
        response = self.client.get(self.url)
        self.assertEqual(response.context["pending"], [])
        self.assertEqual(response.context["decided"], [])
        self.assertContains(response, "No pending predictions")
        self.assertContains(response, "No decided predictions yet")


class MatchDetailViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pass12345")

    def _match(self, label, **kwargs):
        sport = Sport.objects.create(name=f"Sport {label}")
        return future_match(
            sport=sport,
            team_a=Team.objects.create(name=f"A {label}", sport=sport),
            team_b=Team.objects.create(name=f"B {label}", sport=sport),
            **kwargs,
        )

    def _score(self, match, winning_side):
        match.winner = match.team_a if winning_side == "A" else match.team_b
        match.save()
        self.assertTrue(score_match(match.pk))
        match.refresh_from_db()

    def _login(self):
        self.client.login(username="alice", password="pass12345")

    def test_unpublished_match_returns_404(self):
        match = self._match("hidden", is_published=False)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.status_code, 404)

    def test_published_match_returns_200(self):
        match = self._match("core")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.status_code, 200)

    def test_page_shows_teams_sport_kickoff_and_deadline(self):
        match = self._match("core")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "A core")
        self.assertContains(response, "B core")
        self.assertContains(response, "Sport core")
        self.assertContains(response, "Kickoff")
        self.assertContains(response, "Prediction deadline")

    def test_open_match_shows_open_and_predict_link(self):
        match = self._match("open")
        self._login()
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "open")
        self.assertContains(response, "Open")
        self.assertContains(response, reverse("predict", args=[match.pk]))

    def test_locked_match_shows_locked_and_no_predict_link(self):
        match = self._match(
            "locked",
            start_time=timezone.now() - timedelta(minutes=5),
            prediction_deadline=timezone.now() - timedelta(hours=1),
        )
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "locked")
        self.assertContains(response, "Locked")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_cancelled_match_shows_cancelled_and_no_predict_link(self):
        match = self._match("cancelled", status=Match.Status.CANCELLED)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "cancelled")
        self.assertContains(response, "Cancelled")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_live_match_shows_live_state(self):
        match = self._match("live", status=Match.Status.LIVE)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "live")
        self.assertContains(response, "Live")
        self.assertNotContains(response, "Locked")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_completed_scored_match_shows_winner(self):
        match = self._match("done")
        self._score(match, "A")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "completed")
        self.assertContains(response, "Result:")
        self.assertContains(response, match.winner_name())

    def test_correct_prediction_shows_hit_and_plus_ten(self):
        match = self._match("hit")
        Prediction.objects.create(user=self.user, match=match, choice="A")
        self._score(match, "A")
        self._login()
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Hit")
        self.assertContains(response, "+10")

    def test_incorrect_prediction_shows_miss_and_minus_five(self):
        match = self._match("miss")
        Prediction.objects.create(user=self.user, match=match, choice="A")
        self._score(match, "B")
        self._login()
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Miss")
        self.assertContains(response, "-5")

    def test_existing_prediction_shows_pick_and_change_pick(self):
        match = self._match("pick")
        Prediction.objects.create(user=self.user, match=match, choice="A")
        self._login()
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Your pick")
        self.assertContains(response, "A pick")
        self.assertContains(response, "Change pick")

    def test_guest_on_open_match_sees_login_to_predict(self):
        match = self._match("guest")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Log in to predict")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_match_list_links_to_detail_page(self):
        match = self._match("linked")
        response = self.client.get(reverse("match_list"))
        self.assertContains(response, reverse("match_detail", args=[match.pk]))


class LeaderboardViewTests(TestCase):
    """Hardening for templates/predictions/leaderboard.html + the leaderboard view."""

    def _user(self, username, points=0):
        user = User.objects.create_user(username, password="pass12345")
        # A Profile is auto-created by the post_save signal; set its points.
        Profile.objects.filter(user=user).update(points=points)
        return user

    @staticmethod
    def _row_for(content, username):
        """Return the <tr>...</tr> slice of the rendered table body for a username."""
        body = content[content.index("<tbody>"):content.index("</tbody>")]
        at = body.index(f"<td>{username}</td>")
        start = body.rindex("<tr", 0, at)
        stop = body.index("</tr>", at)
        return body[start:stop]

    def test_orders_by_points_highest_first_with_matching_ranks(self):
        self._user("carol", points=30)
        self._user("alice", points=10)
        self._user("bob", points=-5)

        response = self.client.get(reverse("leaderboard"))

        profiles = list(response.context["profiles"])
        self.assertEqual(
            [(p.user.username, p.points) for p in profiles],
            [("carol", 30), ("alice", 10), ("bob", -5)],
        )
        content = response.content.decode()
        self.assertLess(content.index("carol"), content.index("alice"))
        self.assertLess(content.index("alice"), content.index("bob"))
        self.assertIn("<td>1</td>", self._row_for(content, "carol"))
        self.assertIn("<td>2</td>", self._row_for(content, "alice"))
        self.assertIn("<td>3</td>", self._row_for(content, "bob"))

    def test_ties_broken_by_username_ascending(self):
        self._user("zoe", points=15)
        self._user("amy", points=15)

        response = self.client.get(reverse("leaderboard"))

        profiles = list(response.context["profiles"])
        self.assertEqual([p.user.username for p in profiles], ["amy", "zoe"])
        content = response.content.decode()
        self.assertLess(content.index("amy"), content.index("zoe"))

    def test_logged_in_user_row_is_highlighted(self):
        self._user("alice", points=10)
        self._user("bob", points=20)
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("leaderboard"))
        content = response.content.decode()

        self.assertIn("table-warning", self._row_for(content, "alice"))
        self.assertNotIn("table-warning", self._row_for(content, "bob"))

    def test_anonymous_visitor_gets_no_highlighted_row(self):
        self._user("alice", points=10)
        self._user("bob", points=20)

        response = self.client.get(reverse("leaderboard"))

        self.assertNotContains(response, "table-warning")

    def test_leaderboard_reflects_scored_predictions(self):
        correct_user = User.objects.create_user("winner", password="pass12345")
        wrong_user = User.objects.create_user("loser", password="pass12345")
        match = future_match()
        Prediction.objects.create(user=correct_user, match=match, choice="A")
        Prediction.objects.create(user=wrong_user, match=match, choice="B")

        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("leaderboard"))

        profiles = list(response.context["profiles"])
        self.assertEqual(
            [(p.user.username, p.points) for p in profiles],
            [("winner", POINTS_CORRECT), ("loser", POINTS_WRONG)],
        )
        content = response.content.decode()
        self.assertLess(content.index("winner"), content.index("loser"))
        self.assertIn(f"<td>{POINTS_CORRECT}</td>", self._row_for(content, "winner"))
        self.assertIn(f"<td>{POINTS_WRONG}</td>", self._row_for(content, "loser"))


class MatchListViewTests(TestCase):
    """Hardening for the match_list view + templates/predictions/match_list.html."""

    def _match(self, label, **kwargs):
        sport = Sport.objects.create(name=f"Sport {label}")
        return future_match(
            sport=sport,
            team_a=Team.objects.create(name=f"A {label}", sport=sport),
            team_b=Team.objects.create(name=f"B {label}", sport=sport),
            **kwargs,
        )

    def _past_deadline_kwargs(self):
        now = timezone.now()
        return {
            "start_time": now - timedelta(minutes=5),
            "prediction_deadline": now - timedelta(hours=1),
        }

    def test_published_match_is_visible_and_unpublished_is_not(self):
        self._match("shown")
        self._match("hidden", is_published=False)

        response = self.client.get(reverse("match_list"))

        self.assertContains(response, "A shown")
        self.assertNotContains(response, "A hidden")

    def test_future_match_is_open_past_deadline_match_is_closed(self):
        open_match = self._match("open")
        closed_match = self._match("closed", **self._past_deadline_kwargs())

        response = self.client.get(reverse("match_list"))

        open_ids = [m.id for m in response.context["open_matches"]]
        closed_ids = [m.id for m in response.context["closed_matches"]]
        self.assertIn(open_match.id, open_ids)
        self.assertNotIn(open_match.id, closed_ids)
        self.assertIn(closed_match.id, closed_ids)
        self.assertNotIn(closed_match.id, open_ids)

    def test_scored_match_shows_winner_and_scored_badge(self):
        match = self._match("scored")
        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("match_list"))

        self.assertIn(match.id, [m.id for m in response.context["closed_matches"]])
        self.assertContains(response, "Winner:")
        self.assertContains(response, str(match.team_a))
        self.assertContains(response, "Scored")

    def test_locked_match_without_winner_shows_awaiting_message_and_no_scored(self):
        match = self._match("locked", **self._past_deadline_kwargs())

        response = self.client.get(reverse("match_list"))

        self.assertIn(match.id, [m.id for m in response.context["closed_matches"]])
        self.assertContains(response, "Predictions locked. Winner not entered yet.")
        self.assertNotContains(response, "Scored")

    def test_cancelled_match_is_closed_not_open(self):
        # Documents current behaviour: a cancelled match drops out of "open"
        # and lands in "closed" with no cancelled-specific label in the list.
        match = self._match("cancelled", status=Match.Status.CANCELLED)

        response = self.client.get(reverse("match_list"))

        self.assertNotIn(match.id, [m.id for m in response.context["open_matches"]])
        self.assertIn(match.id, [m.id for m in response.context["closed_matches"]])

    def test_cancelled_match_shows_cancelled_status_not_awaiting_winner(self):
        match = self._match("cancelled-ui", status=Match.Status.CANCELLED)

        response = self.client.get(reverse("match_list"))

        self.assertIn(match.id, [m.id for m in response.context["closed_matches"]])
        self.assertContains(response, "Cancelled")
        self.assertContains(response, "no result will be recorded")
        self.assertNotContains(response, "Winner not entered yet.")

    def test_live_match_shows_live_status_not_awaiting_winner(self):
        match = self._match("live-ui", status=Match.Status.LIVE)

        response = self.client.get(reverse("match_list"))

        self.assertIn(match.id, [m.id for m in response.context["closed_matches"]])
        self.assertContains(response, "Live")
        self.assertContains(response, "in progress")
        self.assertNotContains(response, "Winner not entered yet.")

    def test_authenticated_user_sees_their_pick_and_change_label(self):
        match = self._match("pick")
        user = User.objects.create_user("alice", password="pass12345")
        Prediction.objects.create(user=user, match=match, choice="A")
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("match_list"))

        self.assertContains(response, "Your pick")
        self.assertContains(response, "Change pick")

    def test_guest_sees_login_to_predict_and_not_the_predict_link(self):
        match = self._match("guest")

        response = self.client.get(reverse("match_list"))

        self.assertContains(response, "Log in to predict")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_closed_section_empty_state(self):
        self._match("only-open")

        response = self.client.get(reverse("match_list"))

        self.assertEqual(response.context["closed_matches"], [])
        self.assertContains(response, "No closed matches yet.")


class MatchAdminActionTests(TestCase):
    def setUp(self):
        User.objects.create_superuser("root", "root@example.com", "pass12345")
        self.client.login(username="root", password="pass12345")
        self.match = future_match(is_published=False)
        self.url = reverse("admin:predictions_match_changelist")

    def test_publish_action_publishes_selected_matches(self):
        self.client.post(
            self.url,
            {"action": "publish_matches", "_selected_action": [self.match.pk]},
        )
        self.match.refresh_from_db()
        self.assertTrue(self.match.is_published)

    def test_unpublish_action_hides_selected_matches(self):
        self.match.is_published = True
        self.match.save(update_fields=["is_published"])
        self.client.post(
            self.url,
            {"action": "unpublish_matches", "_selected_action": [self.match.pk]},
        )
        self.match.refresh_from_db()
        self.assertFalse(self.match.is_published)


class SettingsSecurityTests(SimpleTestCase):
    """config/settings.py: HTTPS / secure-cookie config only under DEBUG=False.

    These load the settings file in an isolated namespace with a patched
    environment; they assume the repo has no local .env file (it is gitignored).
    """

    SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.py"

    def _load(self, **environment):
        namespace = {"__file__": str(self.SETTINGS_PATH)}
        source = self.SETTINGS_PATH.read_text()
        with mock.patch.dict(os.environ, environment, clear=True):
            exec(compile(source, str(self.SETTINGS_PATH), "exec"), namespace)
        return namespace

    def _production(self, **extra):
        return self._load(
            DEBUG="False",
            SECRET_KEY="x" * 50,
            ALLOWED_HOSTS="example.com",
            **extra,
        )

    def test_local_development_has_no_https_enforcement(self):
        settings = self._load(DEBUG="True")
        self.assertTrue(settings["DEBUG"])
        for name in (
            "SECURE_SSL_REDIRECT",
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "SECURE_HSTS_SECONDS",
            "SECURE_HSTS_INCLUDE_SUBDOMAINS",
            "SECURE_HSTS_PRELOAD",
            "SECURE_PROXY_SSL_HEADER",
            "CSRF_TRUSTED_ORIGINS",
        ):
            self.assertNotIn(name, settings, f"{name} must not be set in development")

    def test_production_enables_https_and_secure_cookies(self):
        settings = self._production()
        self.assertFalse(settings["DEBUG"])
        self.assertTrue(settings["SECURE_SSL_REDIRECT"])
        self.assertTrue(settings["SESSION_COOKIE_SECURE"])
        self.assertTrue(settings["CSRF_COOKIE_SECURE"])
        self.assertEqual(
            settings["SECURE_PROXY_SSL_HEADER"],
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )
        self.assertGreater(settings["SECURE_HSTS_SECONDS"], 0)
        self.assertTrue(settings["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertTrue(settings["SECURE_HSTS_PRELOAD"])
        self.assertEqual(settings["CSRF_TRUSTED_ORIGINS"], [])

    def test_production_security_values_are_environment_overridable(self):
        settings = self._production(
            SECURE_SSL_REDIRECT="False",
            SECURE_HSTS_SECONDS="60",
            SECURE_HSTS_INCLUDE_SUBDOMAINS="False",
            SECURE_HSTS_PRELOAD="False",
            CSRF_TRUSTED_ORIGINS="https://a.example,https://b.example",
        )
        self.assertFalse(settings["SECURE_SSL_REDIRECT"])
        self.assertEqual(settings["SECURE_HSTS_SECONDS"], 60)
        self.assertFalse(settings["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(settings["SECURE_HSTS_PRELOAD"])
        self.assertEqual(
            settings["CSRF_TRUSTED_ORIGINS"],
            ["https://a.example", "https://b.example"],
        )
