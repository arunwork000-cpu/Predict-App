"""Match alerts: web push notifications for the installable app.

When an admin publishes matches, every user who turned on alerts gets one
quiet notification ("4 matches waiting for your prediction") and, where the
device supports it, that number on the app icon -- like an unread count.
The count is personal: open matches the user hasn't predicted yet. Pages
refresh the icon number too (see pwa.js), so it drops as the user predicts.

Needs VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY (see config/settings.py); without
them nothing is sent.
"""
import json
import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from pywebpush import WebPushException, webpush

from . import services
from .models import PushSubscription

logger = logging.getLogger(__name__)

# One notification per device: a newer one replaces the older one.
NOTIFICATION_TAG = "new-matches"
# Push services drop an undelivered message after this long (seconds); a
# phone that's off for a day doesn't need yesterday's count.
TTL = 12 * 60 * 60
# Responses meaning the subscription is gone (app uninstalled, alerts
# turned off in the phone's settings).
GONE_STATUSES = {404, 410}


def is_enabled():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def alert_text(count):
    if count == 1:
        return "1 match waiting for your prediction"
    return f"{count} matches waiting for your prediction"


def notify_new_matches(match_ids):
    """Alert users about newly published matches.

    Users who have already predicted all of these matches (or whose
    subscriptions exist but have nothing open) are skipped. Returns the
    number of users notified.
    """
    if not is_enabled() or not match_ids:
        return 0
    new_matches = services.open_matches().filter(pk__in=match_ids)
    if not new_matches.exists():
        return 0

    subscriptions = {}
    for subscription in PushSubscription.objects.select_related("user"):
        subscriptions.setdefault(subscription.user, []).append(subscription)

    notified = 0
    for user, user_subscriptions in subscriptions.items():
        if not new_matches.exclude(predictions__user=user).exists():
            continue
        count = services.open_matches(user).count()
        payload = json.dumps({
            "title": "WinSports",
            "body": alert_text(count),
            "count": count,
            "url": "/",
            "tag": NOTIFICATION_TAG,
        })
        if any([_send(subscription, payload) for subscription in user_subscriptions]):
            notified += 1
    return notified


def _send(subscription, payload):
    """Send one message; delete the subscription if the device is gone."""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=TTL,
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in GONE_STATUSES:
            subscription.delete()
        else:
            logger.warning("Match alert to %s failed: %s", subscription.user, exc)
        return False
    except Exception:  # network errors etc. must not break publishing
        logger.exception("Match alert to %s failed", subscription.user)
        return False
    subscription.last_sent_at = timezone.now()
    subscription.save(update_fields=["last_sent_at"])
    return True


def notify_new_matches_later(match_ids):
    """Send the alerts after the current transaction commits, in the
    background, so publishing in the admin stays fast however many users
    have alerts on."""
    match_ids = list(match_ids)
    if not is_enabled() or not match_ids:
        return
    transaction.on_commit(lambda: _run_in_background(_notify_and_close, match_ids))


def _notify_and_close(match_ids):
    try:
        notify_new_matches(match_ids)
    except Exception:
        logger.exception("Match alerts failed")
    finally:
        connection.close()  # this thread's own database connection


def _run_in_background(func, *args):
    threading.Thread(target=func, args=args, daemon=True).start()
