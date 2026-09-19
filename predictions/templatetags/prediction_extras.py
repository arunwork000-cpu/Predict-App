from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def team_flag(team, css_class="team-flag"):
    """Render a small flag <img> for `team`, or nothing if it has none.

    Centralised so every page shows the same fixed-size markup and a team
    with no uploaded flag never renders a broken image.
    """
    if not team or not getattr(team, "flag", None):
        return ""
    try:
        url = team.flag.url
    except ValueError:
        return ""
    return format_html(
        '<img src="{}" alt="{} flag" class="{}">',
        url,
        team.name,
        css_class,
    )
