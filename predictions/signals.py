from django.conf import settings
from django.contrib.auth.signals import user_logged_out
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, PushSubscription, generate_referral_code


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    """Every new User gets a Profile so the leaderboard has a points total,
    and a unique referral code so they can immediately share one."""
    if created:
        Profile.objects.create(
            user=instance, referral_code=generate_referral_code()
        )


@receiver(user_logged_out)
def forget_push_subscription(sender, request, user, **kwargs):
    """Stop match alerts on the device that logs out, so the next person
    using it doesn't get this user's alerts. pwa.js puts the device's push
    endpoint in the logout form."""
    endpoint = request.POST.get("push_endpoint") if request else None
    if endpoint and user is not None:
        PushSubscription.objects.filter(endpoint=endpoint, user=user).delete()
