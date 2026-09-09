def user_points(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, "profile", None)
        return {"user_points": profile.points if profile else 0}
    return {"user_points": None}
