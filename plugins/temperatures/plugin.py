"""Read-only hardware temperatures, independent of Komodo."""

from app.plugins import PluginHandle
from plugins.temperatures.temperature_routes import register_temperature_routes


def setup(context):
    register_temperature_routes(context.application, context.settings, context.authenticator,
                                transport=context.transports.get("temperature"))
    return PluginHandle()
