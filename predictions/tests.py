from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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
        Prediction.objects.create(user=self.alice, match=self.match, choice="A")
        Prediction.objects.create(user=self.bob, match=self.match, choice="B")

    def test_correct_and_incorrect_points(self):
        self.match.winner = self.match.team_a
        self.match.save()
        self.assertTrue(score_match(self.match.pk))
        self.alice.profile.refresh_from_db()
        self.bob.profile.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(self.alice.profile.points, POINTS_CORRECT)
        self.assertEqual(self.bob.profile.points, POINTS_WRONG)
        self.assertTrue(self.match.is_scored)

    def test_scoring_is_idempotent(self):
        self.match.winner = self.match.team_a
        self.match.save()
        self.assertTrue(score_match(self.match.pk))
        self.assertFalse(score_match(self.match.pk))
        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.points, POINTS_CORRECT)

    def test_score_without_winner_does_nothing(self):
        self.assertFalse(score_match(self.match.pk))
        self.alice.profile.refresh_from_db()
        self.assertEqual(self.alice.profile.points, 0)

    def test_cannot_change_winner_after_scoring(self):
        self.match.winner = self.match.team_a
        self.match.save()
        score_match(self.match.pk)
        self.match.refresh_from_db()
        self.match.winner = self.match.team_b
        with self.assertRaises(ValidationError):
            self.match.full_clean()


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
