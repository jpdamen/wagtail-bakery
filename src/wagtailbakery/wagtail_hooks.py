"""
Wagtail hooks to register the bakery admin view and menu item.
"""

from django.urls import path, reverse

from wagtail import hooks

from .admin_views import bakery_admin_view


@hooks.register("register_admin_urls")
def register_bakery_admin_url():
    return [
        path("bakery/", bakery_admin_view, name="wagtailbakery_admin"),
    ]


@hooks.register("register_admin_menu_item")
def register_bakery_menu_item():
    from wagtail.admin.menu import MenuItem

    return MenuItem(
        "Static site",
        reverse("wagtailbakery_admin"),
        icon_name="folder-open-inverse",
        order=1000,
    )
