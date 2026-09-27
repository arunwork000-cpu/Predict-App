from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, generate_referral_code


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    """Every new User gets a Profile so the leaderboard has a points total,
    and a unique referral code so they can immediately share one."""
    if created:
        Profile.objects.create(
            user=instance, referral_code=generate_referral_code()
        )
