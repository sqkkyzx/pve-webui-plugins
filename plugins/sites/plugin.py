"""Shared site links, independent of Komodo."""

from app.plugins import PluginHandle
from plugins.sites.site_routes import register_site_routes
from plugins.sites.icon_discovery import SiteIconDiscovery


def setup(context):
    icons = SiteIconDiscovery(transport=context.transports.get("sites"))
    store = register_site_routes(context.application, context.settings, context.authenticator, context.csrf, icon_discovery=icons)

    async def shutdown():
        try:
            await icons.close()
        finally:
            store.close()

    return PluginHandle(shutdown=shutdown)
