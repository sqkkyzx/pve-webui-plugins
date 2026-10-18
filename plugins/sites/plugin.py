"""Shared site links, independent of Komodo."""

from app.plugins import PluginHandle
from plugins.sites.site_routes import register_site_routes


def setup(context):
    store = register_site_routes(context.application, context.settings, context.authenticator, context.csrf)

    async def shutdown():
        store.close()

    return PluginHandle(shutdown=shutdown)
