from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from predictions.importers.base import ProviderError
from predictions.importers.flashlive import FlashLiveProvider
from predictions.importers.registry import get_providers, provider_for
from predictions.models import Sport
from predictions.services import import_fixtures, sync_results


class Command(BaseCommand):
    help = (
        "Import upcoming fixtures (as unpublished matches) and suggested "
        "results from the configured sports data provider. Results are only "
        "suggested: confirm them in the admin to score predictions."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fixtures", action="store_true", help="Import upcoming fixtures.")
        parser.add_argument("--results", action="store_true", help="Fetch results for matches awaiting one.")
        parser.add_argument("--sport", help="Only this sport (e.g. Football). Default: all.")
        parser.add_argument("--days-ahead", type=int, default=7, help="Fixture window in days (max 7).")
        parser.add_argument(
            "--new-day-only",
            action="store_true",
            help="Fetch only the day --days-ahead from today (1 request per sport). "
            "For daily scheduled runs.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would change, then roll back.")
        parser.add_argument("--list-sports", action="store_true", help="Print FlashLive's sport IDs and exit.")
        parser.add_argument(
            "--list-tournaments",
            action="store_true",
            help="Print the tournament names for --sport (default Football) on "
            "--day, for FLASHLIVE_TOURNAMENTS. 1 request.",
        )
        parser.add_argument("--day", type=int, default=0, help="Day for --list-tournaments (0 = today).")

    def handle(self, *args, **options):
        if options["list_sports"]:
            try:
                for sport_id, name in FlashLiveProvider().list_sports():
                    self.stdout.write(f"{sport_id}\t{name}")
            except ProviderError as exc:
                raise CommandError(str(exc)) from exc
            return

        if options["list_tournaments"]:
            provider = FlashLiveProvider()
            sport_name = (options["sport"] or "Football").title()
            if not provider.supports(sport_name):
                raise CommandError(f"No FlashLive sport ID for {sport_name}.")
            try:
                for name, count in provider.list_tournaments(sport_name, options["day"]):
                    plural = "es" if count != 1 else ""
                    self.stdout.write(f"{name}\t({count} match{plural})")
            except ProviderError as exc:
                raise CommandError(str(exc)) from exc
            finally:
                self._report_requests([provider])
            return

        if not (options["fixtures"] or options["results"]):
            raise CommandError("Pass --fixtures, --results, or both.")

        providers = get_providers()
        if not providers:
            raise CommandError("No provider is configured: set RAPIDAPI_KEY.")

        sports = Sport.objects.all()
        if options["sport"]:
            sports = sports.filter(name__iexact=options["sport"])
            if not sports:
                raise CommandError(f"Unknown sport: {options['sport']}")

        failed = False
        with transaction.atomic():
            for sport in sports:
                provider = provider_for(sport.name, providers)
                if provider is None:
                    continue
                try:
                    if options["fixtures"]:
                        self._report_fixtures(
                            sport,
                            import_fixtures(
                                provider,
                                sport,
                                days_ahead=options["days_ahead"],
                                download_images=not options["dry_run"],
                                new_day_only=options["new_day_only"],
                            ),
                        )
                    if options["results"]:
                        self._report_results(sport, sync_results(provider, sport))
                except ProviderError as exc:
                    # Keep going: one sport failing shouldn't block the rest.
                    failed = True
                    self.stderr.write(self.style.ERROR(f"{sport.name}: {exc}"))

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing was saved."))

        self._report_requests(providers)
        if failed:
            raise CommandError("Some sports failed; see errors above.")

    def _report_requests(self, providers):
        used = sum(getattr(p, "requests_made", 0) for p in providers)
        self.stdout.write(f"API requests used this run: {used}")

    def _report_fixtures(self, sport, summary):
        self.stdout.write(
            f"{sport.name} fixtures: {summary['created']} created, "
            f"{summary['updated']} kickoff times updated, "
            f"{summary['skipped']} skipped"
        )
        for name in summary["new_teams"]:
            self.stdout.write(f"  new team (please review): {name}")
        for match in summary["called_off"]:
            self.stdout.write(self.style.WARNING(f"  postponed/cancelled at source: {match}"))

    def _report_results(self, sport, summary):
        self.stdout.write(
            f"{sport.name} results: {summary['suggested']} suggested, awaiting admin confirmation"
        )
        for match in summary["enter_by_hand"]:
            self.stdout.write(
                self.style.WARNING(f"  no result from source, enter it by hand: {match}")
            )
        for match in summary["called_off"]:
            self.stdout.write(self.style.WARNING(f"  postponed/cancelled at source: {match}"))
