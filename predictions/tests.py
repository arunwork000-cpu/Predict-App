import base64
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import MatchAdmin
from .constants import SUPPORTED_SPORTS
from .locations import STATES_BY_COUNTRY
from .models import Match, Prediction, Profile, ScoreAdjustment, Sport, Team
from .services import POINTS_CORRECT, POINTS_WRONG, score_match
from .templatetags.prediction_extras import signed_points, team_flag

# A valid 1x1 transparent PNG, used as dummy upload data for flag tests.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def make_user(username, **kwargs):
    """create_user with a default email.

    Email is mandatory: EmailRequiredMiddleware bounces logged-in users who
    have none to the add-email page. Pass email="" to make one without.
    """
    kwargs.setdefault("email", f"{username}@example.com")
    return User.objects.create_user(username, **kwargs)


def make_flag(name="flag.png"):
    return SimpleUploadedFile(name, TINY_PNG, content_type="image/png")


class MediaIsolatedTestCase(TestCase):
    """Base class for tests that upload files, using a throwaway MEDIA_ROOT
    so test uploads never land in (or pollute) the real media/ directory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="predictions_test_media_")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()


def future_match(**kwargs):
    now = timezone.now()
    # A fresh, uniquely-named sport by default -- "Football"/"Cricket"/etc are
    # reserved names seeded by a data migration, so tests that don't care
    # which sport they use must not collide with those.
    sport = kwargs.pop("sport", None) or Sport.objects.create(
        name=f"Sport {uuid.uuid4().hex[:10]}"
    )
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
        user = make_user("alice", password="pass12345")
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.points, 0)


class ScoringTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice", password="pass12345")
        self.bob = make_user("bob", password="pass12345")
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
        carol = make_user("carol", password="pass12345")
        self._set_winner("A")
        score_match(self.match.pk)
        self.assertEqual(self._points(carol), 0)

    # --- ScoreAdjustment ledger (backs the monthly leaderboard) --------

    def test_scoring_records_one_ledger_row_per_prediction(self):
        self._set_winner("A")
        score_match(self.match.pk)

        alice_delta = ScoreAdjustment.objects.get(user=self.alice, match=self.match).delta
        bob_delta = ScoreAdjustment.objects.get(user=self.bob, match=self.match).delta
        self.assertEqual(alice_delta, POINTS_CORRECT)
        self.assertEqual(bob_delta, POINTS_WRONG)

    def test_rescoring_same_winner_creates_no_extra_ledger_rows(self):
        self._set_winner("A")
        score_match(self.match.pk)
        score_match(self.match.pk)
        score_match(self.match.pk)

        self.assertEqual(
            ScoreAdjustment.objects.filter(user=self.alice, match=self.match).count(), 1
        )

    def test_winner_correction_adds_a_reconciling_ledger_row(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._set_winner("B")
        score_match(self.match.pk)

        rows = list(
            ScoreAdjustment.objects.filter(user=self.alice, match=self.match).order_by(
                "id"
            )
        )
        self.assertEqual([r.delta for r in rows], [POINTS_CORRECT, POINTS_WRONG - POINTS_CORRECT])
        self.assertEqual(sum(r.delta for r in rows), POINTS_WRONG)

    def test_clearing_winner_adds_a_reversing_ledger_row(self):
        self._set_winner("A")
        score_match(self.match.pk)
        self._clear_winner()
        score_match(self.match.pk)

        rows = list(
            ScoreAdjustment.objects.filter(user=self.alice, match=self.match).order_by(
                "id"
            )
        )
        self.assertEqual([r.delta for r in rows], [POINTS_CORRECT, -POINTS_CORRECT])
        self.assertEqual(sum(r.delta for r in rows), 0)

    def test_scoring_uses_match_configured_points_not_global_defaults(self):
        self.match.team_a_win_points = 25
        self.match.team_a_lose_points = -12
        self.match.team_b_win_points = 40
        self.match.team_b_lose_points = -1
        self.match.save()

        self._set_winner("A")
        score_match(self.match.pk)
        # alice picked A (wins), bob picked B (loses).
        self.assertEqual(self._points(self.alice), 25)
        self.assertEqual(self._points(self.bob), -1)

        self._set_winner("B")
        score_match(self.match.pk)
        # alice's A now loses, bob's B now wins.
        self.assertEqual(self._points(self.alice), -12)
        self.assertEqual(self._points(self.bob), 40)

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
        self.staff = make_user(
            "staff", password="pass12345", is_staff=True, is_superuser=True
        )
        self.alice = make_user("alice", password="pass12345")
        self.bob = make_user("bob", password="pass12345")
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
        user = make_user("alice", password="pass12345")
        match = future_match()
        Prediction.objects.create(user=user, match=match, choice="A")
        with self.assertRaises(IntegrityError):
            Prediction.objects.create(user=user, match=match, choice="B")


class PredictViewTests(TestCase):
    def setUp(self):
        self.user = make_user("alice", password="pass12345")
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


def registration_data(**overrides):
    data = {
        "username": "newuser",
        "password1": "StrongPass123",
        "password2": "StrongPass123",
        "country": "India",
        "state": "Kerala",
        "age": "30",
    }
    data.update(overrides)
    data.setdefault("email", f"{data['username']}@example.com")
    return data


class AccountTests(TestCase):
    def test_register_creates_user_profile_and_logs_in(self):
        response = self.client.post(reverse("register"), registration_data())
        self.assertRedirects(response, reverse("match_list"))
        user = User.objects.get(username="newuser")
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.country, "India")
        self.assertEqual(user.profile.state, "Kerala")
        home = self.client.get(reverse("match_list"))
        self.assertContains(home, "newuser")
        self.assertContains(home, "Log out")
        self.assertNotContains(home, 'href="/accounts/login/"')

    def test_register_rejects_duplicate_username(self):
        make_user("taken", password="StrongPass123")
        response = self.client.post(
            reverse("register"), registration_data(username="taken")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="taken").count(), 1)

    def test_login_and_logout(self):
        make_user("alice", password="StrongPass123")
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
        make_user("alice", password="StrongPass123")
        self.client.login(username="alice", password="StrongPass123")
        response = self.client.get(reverse("register"))
        self.assertRedirects(response, reverse("match_list"))

    def test_logout_get_is_not_allowed(self):
        make_user("alice", password="StrongPass123")
        self.client.login(username="alice", password="StrongPass123")
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)


class RegistrationLocationTests(TestCase):
    """Country/State are required, dropdown-only, and cross-validated."""

    def test_registration_form_renders_country_and_state_as_selects(self):
        response = self.client.get(reverse("register"))
        self.assertContains(response, '<select name="country"')
        self.assertContains(response, '<select name="state"')

    def test_missing_country_is_rejected(self):
        response = self.client.post(reverse("register"), registration_data(country=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertIn("country", response.context["form"].errors)

    def test_missing_state_is_rejected(self):
        response = self.client.post(reverse("register"), registration_data(state=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertIn("state", response.context["form"].errors)

    def test_invalid_country_value_is_rejected(self):
        response = self.client.post(
            reverse("register"), registration_data(country="Narnia")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertIn("country", response.context["form"].errors)

    def test_invalid_state_value_is_rejected(self):
        response = self.client.post(
            reverse("register"), registration_data(state="Atlantis")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertIn("state", response.context["form"].errors)

    def test_state_not_belonging_to_selected_country_is_rejected(self):
        # Texas is a real, listed state -- just not one of India's.
        response = self.client.post(
            reverse("register"), registration_data(country="India", state="Texas")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertIn("state", response.context["form"].errors)

    def test_all_nine_countries_accept_a_state_from_their_own_list(self):
        for index, (country, states) in enumerate(STATES_BY_COUNTRY.items()):
            response = self.client.post(
                reverse("register"),
                registration_data(
                    username=f"country{index}", country=country, state=states[0]
                ),
            )
            self.assertRedirects(response, reverse("match_list"))
            user = User.objects.get(username=f"country{index}")
            self.assertEqual(user.profile.country, country)
            self.assertEqual(user.profile.state, states[0])
            self.client.logout()

    def test_representative_subdivision_per_country_is_accepted(self):
        cases = [
            ("India", "Kerala"),
            ("Bahrain", "Capital Governorate"),
            ("Kuwait", "Hawalli"),
            ("Oman", "Muscat"),
            ("Qatar", "Doha"),
            ("Saudi Arabia", "Riyadh"),
            ("United Arab Emirates (UAE)", "Dubai"),
            ("United Kingdom (UK)", "Scotland"),
            ("United States (USA)", "California"),
        ]
        for index, (country, state) in enumerate(cases):
            response = self.client.post(
                reverse("register"),
                registration_data(username=f"rep{index}", country=country, state=state),
            )
            self.assertRedirects(response, reverse("match_list"))
            user = User.objects.get(username=f"rep{index}")
            self.assertEqual(user.profile.country, country)
            self.assertEqual(user.profile.state, state)
            self.client.logout()


class PasswordResetFlowTests(TestCase):
    NEW_PASSWORD = "StrongNewPass123"

    def setUp(self):
        self.user = make_user(
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

    def test_reset_email_names_account_and_uses_project_subject(self):
        self._request_reset()
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Reset your Sports Predictions password")
        self.assertIn("resetuser", message.body)
        self.assertIn("Sports Predictions", message.body)

    def test_reset_email_matches_case_insensitively_and_only_one_account(self):
        make_user("bystander", email="bystander@example.com")
        self._request_reset(email="RESET@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reset@example.com"])

    def test_unknown_email_does_not_reveal_account_and_sends_nothing(self):
        response = self._request_reset(email="nobody@example.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)


class PasswordChangeFlowTests(TestCase):
    OLD_PASSWORD = "oldpass12345"
    NEW_PASSWORD = "StrongNewPass123"

    def setUp(self):
        self.user = make_user(
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
        self.assertContains(response, "No upcoming Football matches")

    def test_leaderboard_empty_state(self):
        response = self.client.get(reverse("leaderboard"))
        self.assertContains(response, "No players yet")

    def test_leaderboard_shows_both_boards_with_headings(self):
        response = self.client.get(reverse("leaderboard"))
        self.assertContains(response, ">All-Time</h2>")
        self.assertContains(response, ">Monthly</h2>")

    def test_old_monthly_url_redirects_to_combined_leaderboard(self):
        response = self.client.get("/leaderboard/monthly/")
        self.assertRedirects(
            response, reverse("leaderboard"), status_code=301, fetch_redirect_response=False
        )


class MatchModelTests(TestCase):
    def setUp(self):
        self.sport = Sport.objects.create(name="Rugby")
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
        other_sport = Sport.objects.create(name="Chess")
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
        other_sport = Sport.objects.create(name="Netball")
        blades = Team.objects.create(name="Blades", sport=other_sport)
        match = Match(**self.valid_kwargs(team_b=blades))
        with self.assertRaises(ValidationError):
            match.full_clean()

    def test_points_fields_default_to_backwards_compatible_values(self):
        match = Match(**self.valid_kwargs())
        match.full_clean()
        match.save()
        self.assertEqual(match.team_a_win_points, 10)
        self.assertEqual(match.team_a_lose_points, -5)
        self.assertEqual(match.team_b_win_points, 10)
        self.assertEqual(match.team_b_lose_points, -5)

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
        self.assertEqual(str(Sport.objects.create(name="Rugby")), "Rugby")

    def test_name_must_be_unique_in_the_database(self):
        Sport.objects.create(name="Rugby")
        with self.assertRaises(IntegrityError):
            Sport.objects.create(name="Rugby")

    def test_duplicate_name_fails_full_clean(self):
        Sport.objects.create(name="Rugby")
        with self.assertRaises(ValidationError):
            Sport(name="Rugby").full_clean()


class TeamModelTests(TestCase):
    def setUp(self):
        self.sport_one = Sport.objects.create(name="Rugby")
        self.sport_two = Sport.objects.create(name="Baseball")

    def test_str_is_name(self):
        team = Team.objects.create(name="Lions", sport=self.sport_one)
        self.assertEqual(str(team), "Lions")

    def test_name_must_be_unique_per_sport(self):
        Team.objects.create(name="Lions", sport=self.sport_one)
        with self.assertRaises(IntegrityError):
            Team.objects.create(name="Lions", sport=self.sport_one)

    def test_same_name_allowed_in_a_different_sport(self):
        Team.objects.create(name="Lions", sport=self.sport_one)
        Team.objects.create(name="Lions", sport=self.sport_two)
        self.assertEqual(Team.objects.filter(name="Lions").count(), 2)

    def test_duplicate_per_sport_fails_full_clean(self):
        Team.objects.create(name="Lions", sport=self.sport_one)
        with self.assertRaises(ValidationError):
            Team(name="Lions", sport=self.sport_one).full_clean()

    def test_sport_is_protected_while_teams_exist(self):
        Team.objects.create(name="Lions", sport=self.sport_one)
        with self.assertRaises(ProtectedError):
            self.sport_one.delete()


class TeamFlagModelAndTagTests(MediaIsolatedTestCase):
    def setUp(self):
        self.sport = Sport.objects.create(name="Rugby")

    def test_flag_is_optional_and_falsy_by_default(self):
        team = Team.objects.create(name="Lions", sport=self.sport)
        self.assertFalse(team.flag)

    def test_team_can_store_an_uploaded_flag(self):
        team = Team.objects.create(name="Lions", sport=self.sport, flag=make_flag())
        team.refresh_from_db()
        self.assertTrue(team.flag)
        self.assertIn("team_flags/", team.flag.name)

    def test_team_flag_tag_renders_nothing_when_no_flag(self):
        team = Team.objects.create(name="Tigers", sport=self.sport)
        self.assertEqual(team_flag(team), "")

    def test_team_flag_tag_renders_nothing_for_none_team(self):
        self.assertEqual(team_flag(None), "")

    def test_team_flag_tag_renders_img_with_url_and_css_class(self):
        team = Team.objects.create(name="Lions", sport=self.sport, flag=make_flag())
        html = team_flag(team)
        self.assertIn("<img", html)
        self.assertIn(team.flag.url, html)
        self.assertIn('class="team-flag"', html)
        self.assertIn("Lions flag", html)


class TeamFlagRenderingTests(MediaIsolatedTestCase):
    """Flags belong to the team, so the same upload must show up on every
    page that mentions that team, and a flagless team must render its name
    with no broken <img>."""

    def setUp(self):
        # Homepage ("/") shows only the Football sport page, so these
        # cross-page rendering checks must use the real seeded Football sport.
        self.sport = Sport.objects.get(name="Football")
        self.team_a = Team.objects.create(
            name="Lions", sport=self.sport, flag=make_flag("a.png")
        )
        self.team_b = Team.objects.create(name="Tigers", sport=self.sport)
        self.match = future_match(
            sport=self.sport, team_a=self.team_a, team_b=self.team_b
        )

    def test_homepage_shows_flag_and_no_broken_image_for_flagless_team(self):
        response = self.client.get(reverse("match_list"))
        content = response.content.decode()
        self.assertIn(self.team_a.flag.url, content)
        self.assertContains(response, "Tigers")
        self.assertNotIn("Tigers flag", content)

    def test_match_detail_shows_flag_and_no_broken_image_for_flagless_team(self):
        response = self.client.get(reverse("match_detail", args=[self.match.pk]))
        content = response.content.decode()
        self.assertIn(self.team_a.flag.url, content)
        self.assertNotIn("Tigers flag", content)

    def test_my_predictions_shows_flag_and_no_broken_image_for_flagless_team(self):
        user = make_user("alice", password="pass12345")
        Prediction.objects.create(user=user, match=self.match, choice="A")
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("my_predictions"))
        content = response.content.decode()
        self.assertIn(self.team_a.flag.url, content)
        self.assertNotIn("Tigers flag", content)

    def test_same_uploaded_flag_appears_in_every_match_for_that_team(self):
        other_match = future_match(
            sport=self.sport,
            team_a=self.team_a,
            team_b=Team.objects.create(name="Bears", sport=self.sport),
        )
        response = self.client.get(reverse("match_list"))
        content = response.content.decode()
        # Each match card renders team_a's flag twice (title + pick panel);
        # team_a appears in two open matches here.
        self.assertEqual(content.count(self.team_a.flag.url), 4)


class TeamAdminFlagTests(MediaIsolatedTestCase):
    def setUp(self):
        User.objects.create_superuser("root", "root@example.com", "pass12345")
        self.client.login(username="root", password="pass12345")
        self.sport = Sport.objects.create(name="Rugby")

    def test_add_form_exposes_flag_upload_field(self):
        response = self.client.get(reverse("admin:predictions_team_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="flag"')
        self.assertContains(response, 'type="file"')

    def test_admin_can_upload_a_flag_when_creating_a_team(self):
        response = self.client.post(
            reverse("admin:predictions_team_add"),
            {"name": "Lions", "sport": self.sport.pk, "flag": make_flag()},
        )
        self.assertEqual(response.status_code, 302)
        team = Team.objects.get(name="Lions")
        self.assertTrue(team.flag)

    def test_changelist_shows_placeholder_when_flag_missing(self):
        Team.objects.create(name="Tigers", sport=self.sport)
        response = self.client.get(reverse("admin:predictions_team_changelist"))
        self.assertContains(response, "no flag uploaded")

    def test_changelist_shows_flag_preview_image_when_present(self):
        team = Team.objects.create(name="Lions", sport=self.sport, flag=make_flag())
        response = self.client.get(reverse("admin:predictions_team_changelist"))
        self.assertContains(response, team.flag.url)


class MyPredictionsViewTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice", password="pass12345")
        self.bob = make_user("bob", password="pass12345")
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

    def test_awaiting_result_prediction_shows_badge_and_stays_pending(self):
        match = self._match("awaiting")
        Prediction.objects.create(user=self.alice, match=match, choice="A")
        match.status = Match.Status.AWAITING_RESULT
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

        self.assertContains(response, "Awaiting result")
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
        self.user = make_user("alice", password="pass12345")

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

    def test_past_deadline_match_shows_awaiting_result_and_no_predict_link(self):
        match = self._match(
            "locked",
            start_time=timezone.now() - timedelta(minutes=5),
            prediction_deadline=timezone.now() - timedelta(hours=1),
        )
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "awaiting")
        self.assertContains(response, "Awaiting result")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_cancelled_match_shows_cancelled_and_no_predict_link(self):
        match = self._match("cancelled", status=Match.Status.CANCELLED)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "cancelled")
        self.assertContains(response, "Cancelled")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_awaiting_result_match_shows_awaiting_state(self):
        match = self._match("awaiting", status=Match.Status.AWAITING_RESULT)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.context["state"], "awaiting")
        self.assertContains(response, "Awaiting result")
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
        # The homepage only shows Football matches, so this one must be Football.
        football = Sport.objects.get(name="Football")
        match = future_match(
            sport=football,
            team_a=Team.objects.create(name="A linked", sport=football),
            team_b=Team.objects.create(name="B linked", sport=football),
        )
        response = self.client.get(reverse("match_list"))
        self.assertContains(response, reverse("match_detail", args=[match.pk]))


class LeaderboardViewTests(TestCase):
    """Hardening for templates/predictions/leaderboard.html + the leaderboard view."""

    def _user(self, username, points=0):
        user = make_user(username, password="pass12345")
        # A Profile is auto-created by the post_save signal; set its points.
        Profile.objects.filter(user=user).update(points=points)
        return user

    @staticmethod
    def _row_for(content, username, section="all-time"):
        """Return the <tr>...</tr> slice of the rendered table body for a username."""
        marker = f'id="leaderboard-{section}"'
        content = content[content.index(marker):]
        body = content[content.index("<tbody>"):content.index("</tbody>")]
        at = body.index(f"<td>{username}</td>")
        start = body.rindex("<tr", 0, at)
        stop = body.index("</tr>", at)
        return body[start:stop]

    @staticmethod
    def _visible_text_tokens(row):
        """Row markup with all tags stripped, split into whitespace-delimited
        tokens -- lets a Points-cell assertion ignore whatever medal <img>
        markup does or doesn't precede the number."""
        return re.sub(r"<[^>]+>", " ", row).split()

    def test_orders_by_points_highest_first_with_matching_ranks(self):
        self._user("carol", points=30)
        self._user("alice", points=10)
        self._user("bob", points=-5)

        response = self.client.get(reverse("leaderboard"))

        profiles = list(response.context["all_time_profiles"])
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

        profiles = list(response.context["all_time_profiles"])
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
        correct_user = make_user("winner", password="pass12345")
        wrong_user = make_user("loser", password="pass12345")
        match = future_match()
        Prediction.objects.create(user=correct_user, match=match, choice="A")
        Prediction.objects.create(user=wrong_user, match=match, choice="B")

        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("leaderboard"))

        profiles = list(response.context["all_time_profiles"])
        self.assertEqual(
            [(p.user.username, p.points) for p in profiles],
            [("winner", POINTS_CORRECT), ("loser", POINTS_WRONG)],
        )
        content = response.content.decode()
        self.assertLess(content.index("winner"), content.index("loser"))
        self.assertIn(
            str(POINTS_CORRECT),
            self._visible_text_tokens(self._row_for(content, "winner")),
        )
        self.assertIn(
            str(POINTS_WRONG),
            self._visible_text_tokens(self._row_for(content, "loser")),
        )

    def test_legacy_profile_with_no_location_shows_em_dash(self):
        self._user("alice", points=10)

        response = self.client.get(reverse("leaderboard"))
        content = response.content.decode()

        self.assertIn("—", self._row_for(content, "alice"))

    def test_registered_user_shows_state_and_country(self):
        self.client.post(
            reverse("register"),
            registration_data(username="arunkumar", country="India", state="Kerala"),
        )
        self.client.logout()

        response = self.client.get(reverse("leaderboard"))
        content = response.content.decode()

        row = self._row_for(content, "arunkumar")
        self.assertIn("Kerala", row)
        self.assertIn("India", row)


class MonthlyLeaderboardTests(TestCase):
    """The monthly board sums ScoreAdjustment rows created this month --
    never Profile.points directly -- so re-scoring can't double-count."""

    def _user(self, username):
        return make_user(username, password="pass12345")

    def _points(self, user):
        user.profile.refresh_from_db()
        return user.profile.points

    def _monthly_totals(self):
        response = self.client.get(reverse("leaderboard"))
        return {
            p.user.username: p.monthly_points
            for p in response.context["monthly_profiles"]
        }

    def test_monthly_total_matches_this_months_scoring(self):
        alice = self._user("alice")
        bob = self._user("bob")
        match = future_match()
        Prediction.objects.create(user=alice, match=match, choice="A")
        Prediction.objects.create(user=bob, match=match, choice="B")

        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        totals = self._monthly_totals()
        self.assertEqual(totals["alice"], POINTS_CORRECT)
        self.assertEqual(totals["bob"], POINTS_WRONG)

    def test_rescoring_same_winner_does_not_double_count(self):
        alice = self._user("alice")
        match = future_match()
        Prediction.objects.create(user=alice, match=match, choice="A")
        match.winner = match.team_a
        match.save()

        self.assertTrue(score_match(match.pk))
        self.assertFalse(score_match(match.pk))
        self.assertFalse(score_match(match.pk))

        self.assertEqual(self._monthly_totals()["alice"], POINTS_CORRECT)

    def test_winner_correction_adjusts_monthly_total_without_double_counting(self):
        alice = self._user("alice")
        bob = self._user("bob")
        match = future_match()
        Prediction.objects.create(user=alice, match=match, choice="A")
        Prediction.objects.create(user=bob, match=match, choice="B")

        match.winner = match.team_a
        match.save()
        score_match(match.pk)

        match.refresh_from_db()
        match.winner = match.team_b
        match.save()
        score_match(match.pk)

        totals = self._monthly_totals()
        # Net for this month: alice went from correct to incorrect, bob the reverse.
        self.assertEqual(totals["alice"], POINTS_WRONG)
        self.assertEqual(totals["bob"], POINTS_CORRECT)
        # All-time Profile.points agrees too, since it all happened this month.
        self.assertEqual(self._points(alice), POINTS_WRONG)
        self.assertEqual(self._points(bob), POINTS_CORRECT)

    def test_clearing_winner_zeroes_out_the_monthly_total(self):
        alice = self._user("alice")
        match = future_match()
        Prediction.objects.create(user=alice, match=match, choice="A")
        match.winner = match.team_a
        match.save()
        score_match(match.pk)

        match.refresh_from_db()
        match.winner = None
        match.save()
        score_match(match.pk)

        self.assertEqual(self._monthly_totals()["alice"], 0)

    def test_adjustment_dated_last_month_does_not_count_this_month(self):
        alice = self._user("alice")
        match = future_match()
        Prediction.objects.create(user=alice, match=match, choice="A")
        match.winner = match.team_a
        match.save()
        score_match(match.pk)

        # Backdate the ledger row, as if this scoring had actually run last month.
        ScoreAdjustment.objects.filter(user=alice, match=match).update(
            created_at=timezone.now() - timedelta(days=40)
        )

        self.assertEqual(self._monthly_totals().get("alice", 0), 0)
        # All-time points are unaffected by which month the ledger row is dated.
        self.assertEqual(self._points(alice), POINTS_CORRECT)

    def test_profile_with_no_activity_this_month_shows_zero(self):
        self._user("carol")
        self.assertEqual(self._monthly_totals()["carol"], 0)

    def test_monthly_leaderboard_shows_location_columns(self):
        self.client.post(
            reverse("register"),
            registration_data(username="arunkumar", country="India", state="Kerala"),
        )
        self.client.logout()

        response = self.client.get(reverse("leaderboard"))
        row = LeaderboardViewTests._row_for(
            response.content.decode(), "arunkumar", section="monthly"
        )

        self.assertIn("Kerala", row)
        self.assertIn("India", row)


class LeaderboardMedalTests(TestCase):
    """Gold/silver/bronze medal images next to the top three rows only."""

    @staticmethod
    def _row_for(content, username, section="all-time"):
        marker = f'id="leaderboard-{section}"'
        content = content[content.index(marker):]
        body = content[content.index("<tbody>"):content.index("</tbody>")]
        at = body.index(f"<td>{username}</td>")
        start = body.rindex("<tr", 0, at)
        stop = body.index("</tr>", at)
        return body[start:stop]

    def _assert_medal(self, row, filename, alt_text):
        self.assertIn(filename, row)
        self.assertIn(f'alt="{alt_text}"', row)
        self.assertIn("medal-icon", row)

    def _assert_no_medal(self, row):
        self.assertNotIn("medal-icon", row)
        self.assertNotIn("gold.svg", row)
        self.assertNotIn("silver.svg", row)
        self.assertNotIn("bronze.svg", row)

    def test_medal_appears_after_the_points_in_the_row(self):
        for index in range(3):
            user = make_user(f"player{index}", password="pass12345")
            Profile.objects.filter(user=user).update(points=100 - index * 10)

        content = self.client.get(reverse("leaderboard")).content.decode()

        for section in ("all-time", "monthly"):
            row = self._row_for(content, "player0", section)
            points_cell = row[row.rindex("<td>"):]
            before_medal = points_cell[: points_cell.index("<img")]
            # The points number comes first, then the medal image ends the cell.
            self.assertRegex(before_medal, r"\d")
            self.assertEqual(points_cell.count("<img"), 1)

    def test_all_time_leaderboard_shows_medals_only_for_top_three(self):
        for index in range(4):
            user = make_user(f"player{index}", password="pass12345")
            Profile.objects.filter(user=user).update(points=100 - index * 10)

        response = self.client.get(reverse("leaderboard"))
        content = response.content.decode()

        self._assert_medal(
            self._row_for(content, "player0"),
            "gold.svg",
            "Gold medal — first place",
        )
        self._assert_medal(
            self._row_for(content, "player1"),
            "silver.svg",
            "Silver medal — second place",
        )
        self._assert_medal(
            self._row_for(content, "player2"),
            "bronze.svg",
            "Bronze medal — third place",
        )
        self._assert_no_medal(self._row_for(content, "player3"))

    def test_monthly_leaderboard_shows_medals_only_for_top_three(self):
        users = [
            make_user(f"m{index}", password="pass12345")
            for index in range(4)
        ]
        match = future_match()
        for user, delta in zip(users, (40, 30, 20, 10)):
            ScoreAdjustment.objects.create(user=user, match=match, delta=delta)

        response = self.client.get(reverse("leaderboard"))
        content = response.content.decode()

        def row(name):
            return self._row_for(content, name, section="monthly")

        self._assert_medal(row("m0"), "gold.svg", "Gold medal — first place")
        self._assert_medal(row("m1"), "silver.svg", "Silver medal — second place")
        self._assert_medal(row("m2"), "bronze.svg", "Bronze medal — third place")
        self._assert_no_medal(row("m3"))

    def test_fewer_than_three_players_shows_no_missing_medal_errors(self):
        user = make_user("solo", password="pass12345")
        Profile.objects.filter(user=user).update(points=5)

        response = self.client.get(reverse("leaderboard"))

        self.assertEqual(response.status_code, 200)
        self._assert_medal(
            self._row_for(response.content.decode(), "solo"),
            "gold.svg",
            "Gold medal — first place",
        )


def sport_match(sport_name, team_a_name="Team A", team_b_name="Team B", **kwargs):
    """A future_match() pinned to one of the four seeded public sports."""
    sport = Sport.objects.get(name=sport_name)
    team_a = kwargs.pop("team_a", None) or Team.objects.create(
        name=team_a_name, sport=sport
    )
    team_b = kwargs.pop("team_b", None) or Team.objects.create(
        name=team_b_name, sport=sport
    )
    return future_match(sport=sport, team_a=team_a, team_b=team_b, **kwargs)


class SportMatchesViewTests(TestCase):
    """Hardening for the sport_matches view + templates/predictions/sport_matches.html.

    The homepage ("/") simply renders the Football page, so it is covered here too.
    """

    def _past_deadline_kwargs(self):
        now = timezone.now()
        return {
            "start_time": now - timedelta(minutes=5),
            "prediction_deadline": now - timedelta(hours=1),
        }

    def test_homepage_renders_the_football_page(self):
        response = self.client.get(reverse("match_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "predictions/sport_matches.html")
        self.assertEqual(response.context["sport_name"], "Football")

    def test_unknown_sport_slug_is_404(self):
        response = self.client.get(reverse("sport_matches", args=["darts"]))
        self.assertEqual(response.status_code, 404)

    def test_each_supported_sport_has_a_working_page(self):
        for name in SUPPORTED_SPORTS:
            response = self.client.get(reverse("sport_matches", args=[name.lower()]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["sport_name"], name)

    def test_published_match_is_visible_and_unpublished_is_not(self):
        sport_match("Football", "A shown", "B shown")
        sport_match("Football", "A hidden", "B hidden", is_published=False)

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertContains(response, "A shown")
        self.assertNotContains(response, "A hidden")

    def test_only_open_matches_of_the_selected_sport_appear(self):
        football_open = sport_match("Football", "FA open", "FB open")
        football_closed = sport_match(
            "Football", "FA closed", "FB closed", **self._past_deadline_kwargs()
        )
        cricket_open = sport_match("Cricket", "CA open", "CB open")

        response = self.client.get(reverse("sport_matches", args=["football"]))
        shown_ids = [m.id for m in response.context["open_matches"]]

        self.assertIn(football_open.id, shown_ids)
        self.assertNotIn(football_closed.id, shown_ids)
        self.assertNotIn(cricket_open.id, shown_ids)
        self.assertContains(response, "FA open")
        self.assertNotContains(response, "FA closed")
        self.assertNotContains(response, "CA open")

    def test_closed_matches_are_not_shown_on_sport_pages(self):
        sport_match("Football", "FA gone", "FB gone", **self._past_deadline_kwargs())

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertNotContains(response, "FA gone")

    def test_scored_match_no_longer_appears_on_sport_page(self):
        match = sport_match("Football", "FA scored", "FB scored")
        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertNotContains(response, "FA scored")

    def test_authenticated_user_sees_their_selected_team_highlighted(self):
        match = sport_match("Football", "FA pick", "FB pick")
        user = make_user("alice", password="pass12345")
        Prediction.objects.create(user=user, match=match, choice="A")
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertContains(response, "Predicted")
        self.assertContains(response, "If win get:")
        self.assertContains(response, "If Lose/Draw get:")

    def test_open_match_shows_predict_the_win_label(self):
        sport_match("Cricket", "CA cta", "CB cta")

        response = self.client.get(reverse("sport_matches", args=["cricket"]))

        self.assertContains(
            response, "Predict the win ( Select your Team / Player )"
        )

    def test_open_match_shows_team_win_lose_points(self):
        match = sport_match("Tennis", "TA points", "TB points")
        match.team_a_win_points = 20
        match.team_a_lose_points = -8
        match.team_b_win_points = 15
        match.team_b_lose_points = -3
        match.save()

        response = self.client.get(reverse("sport_matches", args=["tennis"]))

        self.assertContains(response, "If win get: <strong>+20</strong>")
        self.assertContains(response, "If lose get: <strong>-8</strong>")
        self.assertContains(response, "If win get: <strong>+15</strong>")
        self.assertContains(response, "If lose get: <strong>-3</strong>")

    def test_authenticated_user_can_predict_directly_from_a_sport_page(self):
        match = sport_match("Badminton", "BA inline", "BB inline")
        user = make_user("alice", password="pass12345")
        self.client.login(username="alice", password="pass12345")

        page = self.client.get(reverse("sport_matches", args=["badminton"]))
        # The sport page renders a form that posts straight to the predict
        # endpoint -- no separate predict page needs to be opened.
        self.assertContains(page, 'action="%s"' % reverse("predict", args=[match.pk]))

        response = self.client.post(
            reverse("predict", args=[match.pk]), {"choice": "B"}
        )
        # Predicting on a Badminton match returns to the Badminton page, not Football.
        self.assertRedirects(response, reverse("sport_matches", args=["badminton"]))

        pick = Prediction.objects.get(user=user, match=match)
        self.assertEqual(pick.choice, "B")

        after = self.client.get(reverse("sport_matches", args=["badminton"]))
        self.assertContains(after, "Predicted")

    def test_guest_does_not_see_predict_forms(self):
        sport_match("Football", "FA guest-forms", "FB guest-forms")

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertNotContains(response, "<form")
        self.assertContains(response, "If win get:")
        self.assertContains(response, "If Lose/Draw get:")

    def test_guest_sees_login_to_predict_and_not_the_predict_link(self):
        match = sport_match("Football", "FA guest", "FB guest")

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertContains(response, "Log in to predict")
        self.assertNotContains(response, reverse("predict", args=[match.pk]))

    def test_empty_state_message_names_the_sport(self):
        response = self.client.get(reverse("sport_matches", args=["tennis"]))
        self.assertContains(response, "No upcoming Tennis matches")

    def test_event_name_shown_below_predict_the_win(self):
        sport_match("Football", "FA event", "FB event", event_name="World Cup")

        response = self.client.get(reverse("sport_matches", args=["football"]))
        content = response.content.decode()

        self.assertIn("World Cup", content)
        self.assertLess(content.index("Predict the win ("), content.index("World Cup"))

    def test_blank_event_name_shows_no_empty_label_or_spacing(self):
        sport_match("Football", "FA noevent", "FB noevent")

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<div class="small text-muted"></div>')


class ClosedMatchesViewTests(TestCase):
    """Hardening for the closed_matches view + templates/predictions/closed_matches.html."""

    def _past_deadline_kwargs(self):
        now = timezone.now()
        return {
            "start_time": now - timedelta(minutes=5),
            "prediction_deadline": now - timedelta(hours=1),
        }

    def test_open_match_is_excluded(self):
        sport_match("Football", "FA open", "FB open")

        response = self.client.get(reverse("closed_matches"))

        self.assertNotContains(response, "FA open")

    def test_unpublished_match_is_excluded(self):
        sport_match(
            "Football",
            "FA hidden",
            "FB hidden",
            is_published=False,
            **self._past_deadline_kwargs(),
        )

        response = self.client.get(reverse("closed_matches"))

        self.assertNotContains(response, "FA hidden")

    def test_deadline_passed_match_is_shown(self):
        sport_match("Cricket", "CA locked", "CB locked", **self._past_deadline_kwargs())

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "CA locked")
        self.assertContains(response, "Awaiting result")
        self.assertContains(response, "The result has not been entered yet.")

    def test_finished_scored_match_is_shown_with_winner_and_scored_badge(self):
        match = sport_match("Tennis", "TA scored", "TB scored")
        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "TA scored")
        self.assertContains(response, "Winner:")
        self.assertContains(response, "Scored")

    def test_cancelled_match_is_shown(self):
        sport_match(
            "Badminton", "BA cancelled", "BB cancelled", status=Match.Status.CANCELLED
        )

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "Cancelled")
        self.assertContains(response, "no result will be recorded")

    def test_awaiting_result_match_is_shown(self):
        sport_match(
            "Football", "FA wait", "FB wait", status=Match.Status.AWAITING_RESULT
        )

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "Awaiting result")
        self.assertContains(response, "The result has not been entered yet")

    def test_shows_matches_from_every_supported_sport(self):
        sport_match("Cricket", "CA multi", "CB multi", **self._past_deadline_kwargs())
        sport_match("Tennis", "TA multi", "TB multi", **self._past_deadline_kwargs())
        sport_match(
            "Badminton", "BA multi", "BB multi", **self._past_deadline_kwargs()
        )
        sport_match("Hockey", "HA multi", "HB multi", **self._past_deadline_kwargs())

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "CA multi")
        self.assertContains(response, "TA multi")
        self.assertContains(response, "BA multi")
        self.assertContains(response, "HA multi")

    def test_empty_state(self):
        response = self.client.get(reverse("closed_matches"))
        self.assertContains(response, "No closed matches yet.")

    def test_event_name_shown_on_closed_match_card(self):
        sport_match(
            "Football",
            "FA wc",
            "FB wc",
            event_name="World Cup",
            **self._past_deadline_kwargs(),
        )

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(response, "World Cup")

    def test_blank_event_name_shows_no_empty_label_or_spacing(self):
        sport_match("Football", "FA noevent2", "FB noevent2", **self._past_deadline_kwargs())

        response = self.client.get(reverse("closed_matches"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<div class="small text-muted"></div>')


class PublicNavTests(TestCase):
    def test_navbar_lists_every_public_menu_item(self):
        response = self.client.get(reverse("match_list"))

        for label in SUPPORTED_SPORTS + ["Closed Matches"]:
            self.assertContains(response, label)
        for name in SUPPORTED_SPORTS:
            self.assertContains(response, reverse("sport_matches", args=[name.lower()]))
        self.assertContains(response, reverse("closed_matches"))

    def test_old_generic_brand_and_matches_link_are_gone(self):
        response = self.client.get(reverse("match_list"))
        self.assertNotContains(response, 'class="navbar-brand"')

    def test_predict_now_badge_shown_only_for_sports_with_an_open_match(self):
        sport_match("Football", "PN A", "PN B")

        response = self.client.get(reverse("match_list"))
        content = response.content.decode()

        football_link = content[content.index('href="/sport/football/"'):]
        football_link = football_link[: football_link.index("</a>")]
        self.assertIn("Predict now", football_link)

        cricket_link = content[content.index('href="/sport/cricket/"'):]
        cricket_link = cricket_link[: cricket_link.index("</a>")]
        self.assertNotIn("Predict now", cricket_link)

    def test_predict_now_badge_hidden_when_no_sport_has_open_matches(self):
        response = self.client.get(reverse("match_list"))
        self.assertNotContains(response, "Predict now")

    def test_predict_now_badge_disappears_once_the_only_open_match_is_scored(self):
        match = sport_match("Tennis", "PN scored A", "PN scored B")
        response = self.client.get(reverse("match_list"))
        self.assertIn("Predict now", response.content.decode())

        match.winner = match.team_a
        match.save()
        self.assertTrue(score_match(match.pk))

        response = self.client.get(reverse("match_list"))
        tennis_link = response.content.decode()
        tennis_link = tennis_link[tennis_link.index('href="/sport/tennis/"'):]
        tennis_link = tennis_link[: tennis_link.index("</a>")]
        self.assertNotIn("Predict now", tennis_link)


class DefaultSportsDataMigrationTests(TestCase):
    def test_all_supported_sports_exist(self):
        names = set(Sport.objects.values_list("name", flat=True))
        for expected in SUPPORTED_SPORTS:
            self.assertIn(expected, names)


class HockeySportTests(TestCase):
    """Hockey was added alongside Football/Cricket/Tennis/Badminton."""

    def test_hockey_sport_exists(self):
        self.assertTrue(Sport.objects.filter(name="Hockey").exists())

    def test_hockey_is_in_the_public_sports_menu(self):
        response = self.client.get(reverse("match_list"))
        self.assertContains(response, "Hockey")
        self.assertContains(response, reverse("sport_matches", args=["hockey"]))

    def test_hockey_sport_page_works_and_shows_only_open_hockey_matches(self):
        hockey_open = sport_match("Hockey", "HA open", "HB open")
        hockey_closed = sport_match(
            "Hockey",
            "HA closed",
            "HB closed",
            start_time=timezone.now() - timedelta(minutes=5),
            prediction_deadline=timezone.now() - timedelta(hours=1),
        )
        football_open = sport_match("Football", "FA open", "FB open")

        response = self.client.get(reverse("sport_matches", args=["hockey"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sport_name"], "Hockey")
        shown_ids = [m.id for m in response.context["open_matches"]]
        self.assertIn(hockey_open.id, shown_ids)
        self.assertNotIn(hockey_closed.id, shown_ids)
        self.assertNotIn(football_open.id, shown_ids)

    def test_hockey_match_is_included_in_closed_matches(self):
        sport_match(
            "Hockey",
            "HA locked",
            "HB locked",
            start_time=timezone.now() - timedelta(minutes=5),
            prediction_deadline=timezone.now() - timedelta(hours=1),
        )
        response = self.client.get(reverse("closed_matches"))
        self.assertContains(response, "HA locked")

    def test_hockey_team_can_be_created_in_admin(self):
        User.objects.create_superuser("root", "root@example.com", "pass12345")
        self.client.login(username="root", password="pass12345")
        hockey = Sport.objects.get(name="Hockey")

        response = self.client.post(
            reverse("admin:predictions_team_add"),
            {"name": "Panthers", "sport": hockey.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Team.objects.filter(name="Panthers", sport=hockey).exists())

    def test_hockey_appears_in_the_sport_autocomplete_used_by_team_and_match_admin(self):
        User.objects.create_superuser("root", "root@example.com", "pass12345")
        self.client.login(username="root", password="pass12345")

        response = self.client.get(
            reverse("admin:autocomplete"),
            {
                "app_label": "predictions",
                "model_name": "team",
                "field_name": "sport",
                "term": "Hockey",
            },
        )

        self.assertEqual(response.status_code, 200)
        names = [row["text"] for row in response.json()["results"]]
        self.assertIn("Hockey", names)

    def test_hockey_match_can_be_created_in_admin(self):
        User.objects.create_superuser("root", "root@example.com", "pass12345")
        self.client.login(username="root", password="pass12345")
        hockey = Sport.objects.get(name="Hockey")
        team_a = Team.objects.create(name="Panthers", sport=hockey)
        team_b = Team.objects.create(name="Wolves", sport=hockey)
        now = timezone.now()

        response = self.client.post(
            reverse("admin:predictions_match_add"),
            {
                "sport": hockey.pk,
                "event_name": "",
                "team_a": team_a.pk,
                "team_b": team_b.pk,
                "start_time_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "start_time_1": "12:00:00",
                "prediction_deadline_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "prediction_deadline_1": "11:00:00",
                "status": Match.Status.SCHEDULED,
                "is_published": "on",
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Match.objects.filter(sport=hockey, team_a=team_a, team_b=team_b).exists()
        )


class EventNameTests(TestCase):
    def test_event_name_defaults_to_blank(self):
        match = future_match()
        self.assertEqual(match.event_name, "")

    def test_event_name_shown_on_match_detail_page(self):
        match = sport_match("Football", "FA detail", "FB detail", event_name="Euro Cup")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Euro Cup")

    def test_blank_event_name_not_shown_on_match_detail_page(self):
        match = sport_match("Football", "FA detail2", "FB detail2")
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertNotContains(response, '<dt class="col-sm-3">Event</dt>')


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

    def test_add_form_exposes_all_four_points_fields(self):
        response = self.client.get(reverse("admin:predictions_match_add"))
        self.assertEqual(response.status_code, 200)
        for field in (
            "team_a_win_points",
            "team_a_lose_points",
            "team_b_win_points",
            "team_b_lose_points",
        ):
            self.assertContains(response, field)

    def test_change_form_exposes_all_four_points_fields(self):
        response = self.client.get(
            reverse("admin:predictions_match_change", args=[self.match.pk])
        )
        self.assertEqual(response.status_code, 200)
        for field in (
            "team_a_win_points",
            "team_a_lose_points",
            "team_b_win_points",
            "team_b_lose_points",
        ):
            self.assertContains(response, field)

    def test_add_form_exposes_event_name_field_with_help_text(self):
        response = self.client.get(reverse("admin:predictions_match_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="event_name"')
        self.assertContains(response, "World Cup, Euro Cup, Wimbledon")

    def test_change_form_exposes_event_name_field(self):
        response = self.client.get(
            reverse("admin:predictions_match_change", args=[self.match.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="event_name"')


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


class VisitorTimezoneTests(TestCase):
    """Times render in the zone from the visitor's ``tz`` cookie, falling
    back to India Standard Time when it is missing or invalid."""

    def setUp(self):
        from datetime import datetime, timezone as dt_timezone

        when = datetime(2026, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
        self.match = future_match(start_time=when, prediction_deadline=when)
        self.url = reverse("match_detail", args=[self.match.pk])

    def _get(self, tz=None):
        if tz is not None:
            self.client.cookies["tz"] = tz
        return self.client.get(self.url)

    def test_no_cookie_renders_india_time(self):
        response = self._get()
        self.assertContains(response, "3:30 p.m. IST")

    def test_cookie_renders_visitor_zone(self):
        response = self._get("America/New_York")
        self.assertContains(response, "6 a.m. EDT")
        self.assertNotContains(response, "3:30 p.m. IST")

    def test_invalid_cookie_falls_back_to_india_time(self):
        for bad in ("Bogus/Zone", "../etc/passwd", ""):
            with self.subTest(cookie=bad):
                response = self._get(bad)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "3:30 p.m. IST")

    def test_zone_does_not_leak_between_requests(self):
        self._get("America/New_York")
        self.client.cookies.pop("tz")
        self.assertContains(self._get(), "3:30 p.m. IST")

    def test_monthly_leaderboard_ignores_visitor_zone(self):
        def totals(tz):
            self.client.cookies["tz"] = tz
            response = self.client.get(reverse("leaderboard"))
            return [
                (p.user.username, p.monthly_points)
                for p in response.context["monthly_profiles"]
            ]

        make_user("alice", password="pass12345")
        self.assertEqual(totals("Pacific/Kiritimati"), totals("Pacific/Pago_Pago"))


class DrawTests(TestCase):
    """Draw as a third pick for Football/Cricket/Hockey, with admin-set points."""

    def setUp(self):
        self.alice = make_user("alice", password="pass12345")
        self.bob = make_user("bob", password="pass12345")
        self.carol = make_user("carol", password="pass12345")

    def _predict(self, user, match, choice):
        self.client.force_login(user)
        return self.client.post(
            reverse("predict", args=[match.pk]), {"choice": choice}
        )

    def test_draw_offered_only_for_draw_sports(self):
        self.client.force_login(self.alice)
        for sport_name, expected in [
            ("Football", True),
            ("Cricket", True),
            ("Hockey", True),
            ("Tennis", False),
            ("Badminton", False),
        ]:
            with self.subTest(sport=sport_name):
                sport_match(sport_name)
                response = self.client.get(
                    reverse("sport_matches", args=[sport_name.lower()])
                )
                self.assertEqual(b'value="D"' in response.content, expected)

    def test_can_save_draw_pick_for_football(self):
        match = sport_match("Football")
        self._predict(self.alice, match, "D")
        self.assertEqual(Prediction.objects.get(user=self.alice).choice, "D")

    def test_draw_pick_rejected_for_tennis(self):
        match = sport_match("Tennis")
        self._predict(self.alice, match, "D")
        self.assertFalse(Prediction.objects.filter(user=self.alice).exists())

    def test_draw_result_scores_using_admin_draw_points(self):
        match = sport_match(
            "Football", draw_win_points=25, draw_lose_points=-2
        )
        Prediction.objects.create(user=self.alice, match=match, choice="D")
        Prediction.objects.create(user=self.bob, match=match, choice="A")
        Prediction.objects.create(user=self.carol, match=match, choice="B")

        match.is_draw = True
        match.save()
        self.assertTrue(score_match(match.pk))

        points = {
            p.user.username: p.points_awarded for p in match.predictions.all()
        }
        # Draw pickers win the draw points; team pickers lose their lose points.
        self.assertEqual(points["alice"], 25)
        self.assertEqual(points["bob"], match.team_a_lose_points)
        self.assertEqual(points["carol"], match.team_b_lose_points)
        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.points, 25)
        self.assertFalse(score_match(match.pk))  # idempotent

    def test_draw_pick_loses_when_a_team_wins(self):
        match = sport_match("Cricket", draw_lose_points=-7)
        Prediction.objects.create(user=self.alice, match=match, choice="D")
        match.winner = match.team_a
        match.save()
        score_match(match.pk)
        self.assertEqual(match.predictions.get().points_awarded, -7)

    def test_correcting_draw_to_winner_reconciles_points(self):
        match = sport_match("Hockey", draw_win_points=25, draw_lose_points=-2)
        Prediction.objects.create(user=self.alice, match=match, choice="D")
        match.is_draw = True
        match.save()
        score_match(match.pk)

        match.is_draw = False
        match.winner = match.team_b
        match.save()
        score_match(match.pk)

        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.points, -2)

        match.winner = None
        match.save()
        score_match(match.pk)
        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.points, 0)

    def test_draw_closes_predictions_and_shows_in_closed_matches(self):
        match = sport_match("Football")
        self.assertTrue(match.predictions_open)
        match.is_draw = True
        match.save()
        self.assertFalse(match.predictions_open)
        response = self.client.get(reverse("closed_matches"))
        self.assertContains(response, "Result: <strong>Draw</strong>")

    def test_match_clean_validation(self):
        from django.core.exceptions import ValidationError

        tennis = sport_match("Tennis", is_draw=True)
        with self.assertRaises(ValidationError) as ctx:
            tennis.full_clean()
        self.assertIn("is_draw", ctx.exception.message_dict)

        football = sport_match("Football", is_draw=True)
        football.winner = football.team_a
        with self.assertRaises(ValidationError) as ctx:
            football.full_clean()
        self.assertIn("is_draw", ctx.exception.message_dict)

        ok = sport_match("Hockey", is_draw=True)
        ok.full_clean()

    def test_draw_pick_display_names(self):
        match = sport_match("Football", is_draw=True)
        pick = Prediction.objects.create(user=self.alice, match=match, choice="D")
        self.assertEqual(pick.choice_name(), "Draw")
        self.assertEqual(match.winner_name(), "Draw")

    def test_admin_form_exposes_draw_fields(self):
        admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:predictions_match_add"))
        self.assertEqual(response.status_code, 200)
        for field in ("draw_win_points", "draw_lose_points", "is_draw"):
            self.assertContains(response, f'name="{field}"')

    def test_draw_box_uses_plain_language_labels(self):
        sport_match("Football", draw_win_points=15, draw_lose_points=-3)
        response = self.client.get(reverse("sport_matches", args=["football"]))
        self.assertContains(response, "If Draw get: <strong>+15</strong>")
        self.assertContains(response, "If Win/Lose get: <strong>-3</strong>")


class NoDrawMatchTests(TestCase):
    """Draw points of 0 and 0 mean the match cannot be drawn: hide the Draw box."""

    def setUp(self):
        self.alice = make_user("alice", password="pass12345")
        self.client.force_login(self.alice)

    def test_zero_zero_draw_points_hide_draw_box(self):
        for sport_name in ("Football", "Cricket", "Hockey"):
            with self.subTest(sport=sport_name):
                sport_match(sport_name, draw_win_points=0, draw_lose_points=0)
                response = self.client.get(
                    reverse("sport_matches", args=[sport_name.lower()])
                )
                self.assertNotContains(response, 'value="D"')
                self.assertNotContains(response, "If Draw get")
                self.assertNotContains(response, "Lose/Draw")
                self.assertContains(response, "If lose get:")
                self.assertContains(response, "col-6")
                self.assertNotContains(response, "col-4")

    def test_one_nonzero_draw_point_still_shows_draw_box(self):
        for win, lose in ((5, 0), (0, -3)):
            with self.subTest(win=win, lose=lose):
                match = sport_match(
                    "Football",
                    f"TA {win}",
                    f"TB {win}",
                    draw_win_points=win,
                    draw_lose_points=lose,
                )
                response = self.client.get(reverse("sport_matches", args=["football"]))
                self.assertContains(response, 'value="D"')
                match.delete()

    def test_draw_pick_rejected_when_draw_points_zero(self):
        match = sport_match("Football", draw_win_points=0, draw_lose_points=0)
        self.client.post(reverse("predict", args=[match.pk]), {"choice": "D"})
        self.assertFalse(Prediction.objects.filter(user=self.alice).exists())

    def test_team_picks_still_work_when_draw_points_zero(self):
        match = sport_match("Cricket", draw_win_points=0, draw_lose_points=0)
        self.client.post(reverse("predict", args=[match.pk]), {"choice": "A"})
        self.assertEqual(Prediction.objects.get(user=self.alice).choice, "A")

    def test_match_can_mix_draw_and_no_draw(self):
        sport_match("Football", "TA1", "TB1", draw_win_points=0, draw_lose_points=0)
        sport_match("Football", "TA2", "TB2")
        response = self.client.get(reverse("sport_matches", args=["football"]))
        self.assertContains(response, 'value="D"', count=1)

    def test_cannot_mark_result_as_draw_when_draw_points_zero(self):
        match = sport_match("Football", draw_win_points=0, draw_lose_points=0)
        match.is_draw = True
        with self.assertRaises(ValidationError):
            match.full_clean()


class AutoFinishedStatusTests(TestCase):
    """Entering a result moves a Scheduled/Live match to Finished."""

    def test_winner_marks_scheduled_match_finished(self):
        match = sport_match("Football")
        self.assertEqual(match.status, Match.Status.SCHEDULED)
        match.winner = match.team_a
        match.save()
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.FINISHED)

    def test_draw_marks_awaiting_match_finished(self):
        match = sport_match("Cricket", status=Match.Status.AWAITING_RESULT)
        match.is_draw = True
        match.save()
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.FINISHED)

    def test_no_result_leaves_status_alone(self):
        match = sport_match("Hockey")
        match.event_name = "Cup"
        match.save()
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.SCHEDULED)

    def test_cancelled_match_stays_cancelled(self):
        match = sport_match("Football", status=Match.Status.CANCELLED)
        match.winner = match.team_a
        match.save()
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.CANCELLED)

    def test_save_with_update_fields_still_persists_status(self):
        match = sport_match("Football")
        match.winner = match.team_a
        match.save(update_fields=["winner"])
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.FINISHED)

    def test_admin_entering_result_finishes_match(self):
        admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(admin_user)
        match = sport_match("Football")
        now = timezone.now()
        response = self.client.post(
            reverse("admin:predictions_match_change", args=[match.pk]),
            {
                "sport": match.sport_id,
                "event_name": "",
                "team_a": match.team_a_id,
                "team_b": match.team_b_id,
                "start_time_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "start_time_1": "12:00:00",
                "prediction_deadline_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "prediction_deadline_1": "11:00:00",
                "status": Match.Status.SCHEDULED,
                "winner": match.team_a_id,
                "is_published": "on",
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )
        self.assertEqual(response.status_code, 302)
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.FINISHED)
        self.assertTrue(match.is_scored)


class AwaitingResultStatusTests(TestCase):
    """Scheduled matches past their prediction deadline become Awaiting result."""

    def _past_match(self, **kwargs):
        now = timezone.now()
        return sport_match(
            "Football",
            start_time=now - timedelta(hours=3),
            prediction_deadline=now - timedelta(hours=3),
            **kwargs,
        )

    def test_sync_moves_past_deadline_scheduled_match(self):
        from .services import sync_match_statuses

        past = self._past_match()
        future = sport_match("Football", team_a_name="X", team_b_name="Y")
        self.assertEqual(sync_match_statuses(), 1)
        past.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(past.status, Match.Status.AWAITING_RESULT)
        self.assertEqual(future.status, Match.Status.SCHEDULED)
        self.assertEqual(sync_match_statuses(), 0)  # idempotent

    def test_sync_ignores_cancelled_and_finished_matches(self):
        from .services import sync_match_statuses

        finished = self._past_match(status=Match.Status.FINISHED)
        cancelled = sport_match(
            "Football",
            team_a_name="C1",
            team_b_name="C2",
            status=Match.Status.CANCELLED,
            start_time=timezone.now() - timedelta(hours=3),
            prediction_deadline=timezone.now() - timedelta(hours=3),
        )
        sync_match_statuses()
        finished.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertEqual(finished.status, Match.Status.FINISHED)
        self.assertEqual(cancelled.status, Match.Status.CANCELLED)

    def test_public_pages_trigger_sync_and_show_badge(self):
        match = self._past_match()
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Awaiting result")
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.AWAITING_RESULT)
        response = self.client.get(reverse("closed_matches"))
        self.assertContains(response, "The result has not been entered yet")

    def test_admin_list_shows_awaiting_result(self):
        self._past_match()
        admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:predictions_match_changelist"))
        self.assertContains(response, "Awaiting result")

    def test_entering_result_finishes_awaiting_match(self):
        from .services import sync_match_statuses

        match = self._past_match()
        sync_match_statuses()
        match.refresh_from_db()
        match.winner = match.team_a
        match.save()
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.FINISHED)

    def test_open_match_is_not_affected(self):
        match = sport_match("Football")
        self.client.get(reverse("sport_matches", args=["football"]))
        match.refresh_from_db()
        self.assertEqual(match.status, Match.Status.SCHEDULED)
        self.assertTrue(match.predictions_open)


class RetireLiveStatusMigrationTests(TestCase):
    """0012 converts leftover Live matches to a status that still exists."""

    def test_live_rows_are_converted(self):
        import importlib

        from django.apps import apps as django_apps

        migration = importlib.import_module(
            "predictions.migrations.0012_match_awaiting_result"
        )
        now = timezone.now()
        open_live = sport_match("Football", "OpenA", "OpenB")
        past_live = sport_match(
            "Football", "PastA", "PastB",
            start_time=now - timedelta(hours=3),
            prediction_deadline=now - timedelta(hours=3),
        )
        done_live = sport_match("Football", "DoneA", "DoneB")
        Match.objects.filter(pk__in=[open_live.pk, past_live.pk, done_live.pk]).update(
            status="live"
        )
        Match.objects.filter(pk=done_live.pk).update(winner=done_live.team_a)

        migration.mark_awaiting_result(django_apps, None)

        for match, expected in [
            (open_live, "scheduled"),
            (past_live, "awaiting_result"),
            (done_live, "finished"),
        ]:
            match.refresh_from_db()
            self.assertEqual(match.status, expected)


class DatabaseFlagStorageTests(TestCase):
    """Uploaded flags live in the database and are served from /media/."""

    def setUp(self):
        self.sport = Sport.objects.create(name="Storage Sport")

    def test_uploaded_flag_is_stored_and_served_from_database(self):
        from .models import StoredFile

        team = Team.objects.create(name="Lions", sport=self.sport, flag=make_flag())
        self.assertTrue(StoredFile.objects.filter(name=team.flag.name).exists())
        response = self.client.get(team.flag.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content, TINY_PNG)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_unknown_media_path_returns_404(self):
        self.assertEqual(self.client.get("/media/team_flags/nope.png").status_code, 404)

    def test_same_filename_does_not_overwrite_existing_flag(self):
        a = Team.objects.create(name="A", sport=self.sport, flag=make_flag("f.png"))
        b = Team.objects.create(name="B", sport=self.sport, flag=make_flag("f.png"))
        self.assertNotEqual(a.flag.name, b.flag.name)
        self.assertEqual(self.client.get(a.flag.url).status_code, 200)
        self.assertEqual(self.client.get(b.flag.url).status_code, 200)

    def test_deleting_flag_removes_stored_file(self):
        from .models import StoredFile

        team = Team.objects.create(name="Lions", sport=self.sport, flag=make_flag())
        name = team.flag.name
        team.flag.delete(save=True)
        self.assertFalse(StoredFile.objects.filter(name=name).exists())


class RegistrationEmailAgeTests(TestCase):
    """Required Email, required Age (18-99) and required-field stars."""

    def _register(self, **overrides):
        return self.client.post(reverse("register"), registration_data(**overrides))

    def test_email_is_required(self):
        for missing in ("", "   "):
            with self.subTest(email=missing):
                response = self._register(email=missing)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_email_is_saved_when_given(self):
        self._register(email="fan@example.com")
        self.assertEqual(User.objects.get(username="newuser").email, "fan@example.com")

    def test_email_is_saved_lower_cased(self):
        self._register(email="Fan@Example.COM")
        self.assertEqual(User.objects.get(username="newuser").email, "fan@example.com")

    def test_duplicate_email_is_rejected_case_insensitively(self):
        make_user("first", email="fan@example.com")
        response = self._register(email="FAN@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email already exists.")
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_invalid_email_is_rejected(self):
        response = self._register(email="not-an-email")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_age_is_saved_on_profile(self):
        self._register(age="45")
        self.assertEqual(User.objects.get(username="newuser").profile.age, 45)

    def test_age_is_required(self):
        response = self._register(age="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_age_out_of_range_is_rejected(self):
        for bad in ("17", "100", "abc", "0"):
            with self.subTest(age=bad):
                response = self._register(age=bad)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_age_boundaries_are_accepted(self):
        for i, age in enumerate(("18", "99")):
            with self.subTest(age=age):
                self.client.logout()
                self._register(username=f"edge{i}", age=age)
                self.assertEqual(User.objects.get(username=f"edge{i}").profile.age, int(age))

    def test_page_shows_age_dropdown_18_to_99_and_prize_note(self):
        response = self.client.get(reverse("register"))
        content = response.content.decode()
        self.assertIn('<option value="18">18</option>', content)
        self.assertIn('<option value="99">99</option>', content)
        self.assertNotIn('<option value="17">', content)
        self.assertNotIn('<option value="100">', content)
        self.assertContains(
            response,
            "Required. Used to reset your password and to contact prize winners.",
        )

    def test_all_fields_are_starred_as_required(self):
        content = self.client.get(reverse("register")).content.decode()
        for field in (
            "username", "email", "password1", "password2", "country", "state", "age"
        ):
            with self.subTest(field=field):
                label = re.search(
                    rf'<label[^>]*for="id_{field}"[^>]*>(.*?)</label>', content, re.S
                )
                self.assertIn("*", label.group(1))


class EmailRequiredTests(TestCase):
    """Users without an email are sent to the add-email page."""

    def setUp(self):
        self.user = make_user("old", email="", password="StrongPass123")
        self.client.login(username="old", password="StrongPass123")
        self.add_email_url = reverse("add_email")

    def test_user_without_email_is_redirected_and_returns_to_target(self):
        response = self.client.get(reverse("leaderboard"))
        self.assertRedirects(
            response,
            f"{self.add_email_url}?next={reverse('leaderboard')}",
        )

    def test_post_is_redirected_without_next(self):
        response = self.client.post(reverse("predict", args=[1]))
        self.assertRedirects(response, self.add_email_url)

    def test_exempt_pages_do_not_redirect(self):
        self.assertEqual(self.client.get(self.add_email_url).status_code, 200)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("match_list"))

    def test_admin_is_exempt(self):
        # A non-staff user hitting the admin is bounced to the admin login,
        # never to the add-email page.
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(self.add_email_url, response.url)

    def test_user_with_email_is_not_redirected(self):
        self.client.logout()
        make_user("fine", password="StrongPass123")
        self.client.login(username="fine", password="StrongPass123")
        self.assertEqual(self.client.get(reverse("leaderboard")).status_code, 200)

    def test_guest_is_not_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("leaderboard")).status_code, 200)

    def test_saving_email_lets_user_through_to_next(self):
        response = self.client.post(
            self.add_email_url,
            {"email": "Old@Example.com", "next": reverse("leaderboard")},
        )
        self.assertRedirects(response, reverse("leaderboard"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.com")
        self.assertEqual(self.client.get(reverse("leaderboard")).status_code, 200)

    def test_email_used_by_another_account_is_rejected(self):
        make_user("other", email="taken@example.com")
        response = self.client.post(self.add_email_url, {"email": "TAKEN@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email already exists.")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_blank_or_invalid_email_is_rejected(self):
        for bad in ("", "not-an-email"):
            with self.subTest(email=bad):
                response = self.client.post(self.add_email_url, {"email": bad})
                self.assertEqual(response.status_code, 200)
                self.user.refresh_from_db()
                self.assertEqual(self.user.email, "")

    def test_offsite_next_is_ignored(self):
        response = self.client.post(
            self.add_email_url,
            {"email": "old@example.com", "next": "https://evil.example/"},
        )
        self.assertRedirects(response, reverse("match_list"))

    def test_user_who_already_has_email_is_bounced_off_the_page(self):
        self.user.email = "old@example.com"
        self.user.save()
        self.assertRedirects(self.client.get(self.add_email_url), reverse("match_list"))

    def test_add_email_requires_login(self):
        self.client.logout()
        response = self.client.get(self.add_email_url)
        self.assertRedirects(
            response, f"{reverse('login')}?next={self.add_email_url}"
        )


class MatchAdminTeamsBySportTests(TestCase):
    """Match admin: team dropdowns follow the chosen sport."""

    def setUp(self):
        from .admin import MatchAdminForm

        self.form_class = MatchAdminForm
        self.football = Sport.objects.get(name="Football")
        self.cricket = Sport.objects.get(name="Cricket")
        self.f1 = Team.objects.create(name="F One", sport=self.football)
        self.f2 = Team.objects.create(name="F Two", sport=self.football)
        self.c1 = Team.objects.create(name="C One", sport=self.cricket)
        self.c2 = Team.objects.create(name="C Two", sport=self.cricket)
        self.admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(self.admin_user)

    def _ids(self, form, field):
        return set(form.fields[field].queryset.values_list("pk", flat=True))

    def test_new_match_form_has_no_teams_until_a_sport_is_chosen(self):
        form = self.form_class()
        self.assertEqual(self._ids(form, "team_a"), set())
        self.assertEqual(self._ids(form, "team_b"), set())
        self.assertEqual(form.fields["team_a"].empty_label, "Select a sport first")

    def test_bound_form_limits_teams_to_the_chosen_sport(self):
        form = self.form_class({"sport": self.football.pk})
        self.assertEqual(self._ids(form, "team_a"), {self.f1.pk, self.f2.pk})
        self.assertEqual(self._ids(form, "team_b"), {self.f1.pk, self.f2.pk})

    def test_existing_match_form_shows_its_sports_teams_and_two_winner_choices(self):
        match = sport_match("Football", team_a=self.f1, team_b=self.f2)
        form = self.form_class(instance=match)
        self.assertEqual(self._ids(form, "team_a"), {self.f1.pk, self.f2.pk})
        self.assertEqual(self._ids(form, "winner"), {self.f1.pk, self.f2.pk})

    def test_team_from_another_sport_is_rejected(self):
        now = timezone.now()
        response = self.client.post(
            reverse("admin:predictions_match_add"),
            {
                "sport": self.football.pk,
                "event_name": "",
                "team_a": self.f1.pk,
                "team_b": self.c1.pk,  # a cricket team
                "start_time_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "start_time_1": "12:00:00",
                "prediction_deadline_0": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "prediction_deadline_1": "11:00:00",
                "status": Match.Status.SCHEDULED,
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Match.objects.exists())

    def test_add_page_loads_deadline_script_and_keeps_fields_editable(self):
        response = self.client.get(reverse("admin:predictions_match_add"))
        self.assertContains(response, "predictions/admin/match_deadline.js")
        self.assertContains(response, "predictions/admin/match_tomorrow.js")
        html = response.content.decode()
        for field in (
            "start_time_0",
            "start_time_1",
            "prediction_deadline_0",
            "prediction_deadline_1",
        ):
            with self.subTest(field=field):
                tag = re.search(rf'<input[^>]*id="id_{field}"[^>]*>', html).group(0)
                self.assertNotIn("readonly", tag)
                self.assertNotIn("disabled", tag)

    def test_deadline_can_differ_from_start_time(self):
        day = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.client.post(
            reverse("admin:predictions_match_add"),
            {
                "sport": self.football.pk,
                "event_name": "",
                "team_a": self.f1.pk,
                "team_b": self.f2.pk,
                "start_time_0": day,
                "start_time_1": "12:00:00",
                "prediction_deadline_0": day,
                "prediction_deadline_1": "11:00:00",
                "status": Match.Status.SCHEDULED,
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )
        match = Match.objects.get()
        self.assertEqual(
            timezone.localtime(match.start_time).strftime("%H:%M"), "12:00"
        )
        self.assertEqual(
            timezone.localtime(match.prediction_deadline).strftime("%H:%M"), "11:00"
        )

    def test_add_page_embeds_sport_to_teams_map_and_script(self):
        response = self.client.get(reverse("admin:predictions_match_add"))
        self.assertContains(response, "predictions/admin/match_teams.js")
        self.assertContains(response, "data-teams=")
        mapping = json.loads(
            re.search(r'data-teams="([^"]*)"', response.content.decode())
            .group(1)
            .replace("&quot;", '"')
        )
        self.assertEqual(
            sorted(t[1] for t in mapping[str(self.football.pk)]), ["F One", "F Two"]
        )
        self.assertEqual(
            sorted(t[1] for t in mapping[str(self.cricket.pk)]), ["C One", "C Two"]
        )


class IndiaTimeZoneTests(TestCase):
    """Site default is IST: admin entry, leaderboard month, and fallback."""

    def test_site_time_zone_is_india(self):
        from django.conf import settings

        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")

    def test_admin_ignores_visitor_zone_cookie_and_uses_ist(self):
        from datetime import datetime, timezone as dt_timezone

        admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(admin_user)
        self.client.cookies["tz"] = "America/New_York"
        when = datetime(2026, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
        match = future_match(start_time=when, prediction_deadline=when)
        response = self.client.get(
            reverse("admin:predictions_match_change", args=[match.pk])
        )
        # 10:00 UTC is 15:30 in India; the admin form shows IST, not New York.
        self.assertContains(response, 'value="15:30:00"')
        self.assertNotContains(response, 'value="06:00:00"')

    def test_entering_time_in_admin_is_read_as_ist(self):
        admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(admin_user)
        sport = Sport.objects.get(name="Football")
        a = Team.objects.create(name="IA", sport=sport)
        b = Team.objects.create(name="IB", sport=sport)
        response = self.client.post(
            reverse("admin:predictions_match_add"),
            {
                "sport": sport.pk,
                "event_name": "",
                "team_a": a.pk,
                "team_b": b.pk,
                "start_time_0": "2030-01-10",
                "start_time_1": "18:00:00",
                "prediction_deadline_0": "2030-01-10",
                "prediction_deadline_1": "17:00:00",
                "status": Match.Status.SCHEDULED,
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )
        self.assertEqual(response.status_code, 302)
        match = Match.objects.get(team_a=a)
        # 18:00 IST is 12:30 UTC.
        self.assertEqual((match.start_time.hour, match.start_time.minute), (12, 30))

    def test_monthly_leaderboard_month_starts_at_midnight_ist(self):
        from datetime import datetime, timezone as dt_timezone

        user = make_user("indian", password="pass12345")
        match = future_match()
        # IST midnight on 1 Sept 2026 is 18:30 UTC on 31 Aug.
        before = ScoreAdjustment.objects.create(user=user, match=match, delta=7)
        inside = ScoreAdjustment.objects.create(user=user, match=match, delta=5)
        ScoreAdjustment.objects.filter(pk=before.pk).update(
            created_at=datetime(2026, 8, 31, 18, 0, tzinfo=dt_timezone.utc)
        )
        ScoreAdjustment.objects.filter(pk=inside.pk).update(
            created_at=datetime(2026, 8, 31, 19, 0, tzinfo=dt_timezone.utc)
        )
        fake_now = datetime(2026, 9, 15, 12, 0, tzinfo=dt_timezone.utc)
        for cookie in ("America/Los_Angeles", "Pacific/Auckland", "Asia/Kolkata"):
            with self.subTest(visitor_zone=cookie):
                self.client.cookies["tz"] = cookie
                with mock.patch(
                    "predictions.views.timezone.now", return_value=fake_now
                ):
                    response = self.client.get(reverse("leaderboard"))
                totals = {
                    p.user.username: p.monthly_points
                    for p in response.context["monthly_profiles"]
                }
                self.assertEqual(totals["indian"], 5)


class AdminDateFormatTests(TestCase):
    """Admin date fields use DD-MMM-YYYY (e.g. 10-Mar-2026)."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser("boss", password="pass12345")
        self.client.force_login(self.admin_user)
        self.sport = Sport.objects.get(name="Football")
        self.a = Team.objects.create(name="DA", sport=self.sport)
        self.b = Team.objects.create(name="DB", sport=self.sport)

    def _post(self, date_text):
        return self.client.post(
            reverse("admin:predictions_match_add"),
            {
                "sport": self.sport.pk,
                "event_name": "",
                "team_a": self.a.pk,
                "team_b": self.b.pk,
                "start_time_0": date_text,
                "start_time_1": "18:00:00",
                "prediction_deadline_0": date_text,
                "prediction_deadline_1": "17:00:00",
                "status": Match.Status.SCHEDULED,
                "team_a_win_points": 10,
                "team_a_lose_points": -5,
                "team_b_win_points": 10,
                "team_b_lose_points": -5,
                "draw_win_points": 10,
                "draw_lose_points": -5,
            },
        )

    def test_change_page_shows_dates_as_dd_mmm_yyyy(self):
        from datetime import datetime, timezone as dt_timezone

        when = datetime(2026, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
        match = future_match(start_time=when, prediction_deadline=when)
        response = self.client.get(
            reverse("admin:predictions_match_change", args=[match.pk])
        )
        self.assertContains(response, 'name="start_time_0" value="10-Mar-2026"')
        self.assertContains(response, 'name="prediction_deadline_0" value="10-Mar-2026"')

    def test_dd_mmm_yyyy_input_is_accepted(self):
        response = self._post("10-Jan-2030")
        self.assertEqual(response.status_code, 302)
        match = Match.objects.get(team_a=self.a)
        # 18:00 IST on 10 Jan 2030 is 12:30 UTC the same day.
        self.assertEqual(
            (match.start_time.year, match.start_time.month, match.start_time.day),
            (2030, 1, 10),
        )

    def test_iso_input_is_still_accepted(self):
        self.assertEqual(self._post("2030-01-10").status_code, 302)
        self.assertTrue(Match.objects.filter(team_a=self.a).exists())

    def test_invalid_month_name_is_rejected(self):
        response = self._post("31-Foo-2030")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Match.objects.exists())

    def test_admin_list_shows_dd_mmm_yyyy(self):
        from datetime import datetime, timezone as dt_timezone

        when = datetime(2026, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
        future_match(start_time=when, prediction_deadline=when)
        response = self.client.get(reverse("admin:predictions_match_changelist"))
        self.assertContains(response, "10-Mar-2026, 15:30")


class LoseDrawLabelTests(TestCase):
    """Team boxes say "If Lose/Draw get" only where a draw is possible."""

    def _page(self, sport_name):
        sport_match(sport_name, "TA", "TB")
        return self.client.get(reverse("sport_matches", args=[sport_name.lower()]))

    def test_draw_sports_use_lose_draw_label(self):
        for sport_name in ("Football", "Cricket", "Hockey"):
            with self.subTest(sport=sport_name):
                response = self._page(sport_name)
                self.assertContains(response, "If Lose/Draw get:")
                self.assertNotContains(response, "If lose get:")

    def test_sports_without_draws_keep_lose_label(self):
        for sport_name in ("Tennis", "Badminton"):
            with self.subTest(sport=sport_name):
                response = self._page(sport_name)
                self.assertContains(response, "If lose get:")
                self.assertNotContains(response, "Lose/Draw")


class MatchTitleAndPointsMarkupTests(TestCase):
    """Separate team links with a plain "Vs", and bold signed points."""

    def test_signed_points_filter(self):
        self.assertEqual(signed_points(10), "+10")
        self.assertEqual(signed_points(-5), "-5")
        self.assertEqual(signed_points(0), "0")

    def test_sport_page_links_each_team_separately_with_plain_vs(self):
        match = sport_match("Football", "TA link", "TB link")
        url = reverse("match_detail", args=[match.pk])

        response = self.client.get(reverse("sport_matches", args=["football"]))

        self.assertContains(
            response, f'<a href="{url}">TA link</a> Vs <a href="{url}">TB link</a>'
        )
        self.assertNotContains(response, "TA link vs")

    def test_closed_matches_links_each_team_separately_with_plain_vs(self):
        match = sport_match(
            "Football",
            "CA link",
            "CB link",
            start_time=timezone.now() - timedelta(minutes=5),
            prediction_deadline=timezone.now() - timedelta(hours=1),
        )
        url = reverse("match_detail", args=[match.pk])

        response = self.client.get(reverse("closed_matches"))

        self.assertContains(
            response, f'<a href="{url}">CA link</a> Vs <a href="{url}">CB link</a>'
        )

    def test_points_are_bold_and_signed_for_guests_and_users(self):
        sport_match(
            "Football",
            team_a_win_points=10,
            team_a_lose_points=-5,
            draw_win_points=7,
            draw_lose_points=-2,
        )
        make_user("alice", password="pass12345")
        for logged_in in (False, True):
            with self.subTest(logged_in=logged_in):
                if logged_in:
                    self.client.login(username="alice", password="pass12345")
                response = self.client.get(reverse("sport_matches", args=["football"]))
                self.assertContains(response, "If win get: <strong>+10</strong>")
                self.assertContains(response, "If Lose/Draw get: <strong>-5</strong>")
                self.assertContains(response, "If Draw get: <strong>+7</strong>")
                self.assertContains(response, "If Win/Lose get: <strong>-2</strong>")
