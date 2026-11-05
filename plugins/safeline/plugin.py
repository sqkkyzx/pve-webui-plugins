"""Optional SafeLine lifecycle; no credentials, database or clients when disabled."""

from pathlib import Path

from app.plugins import PluginHandle
from plugins.safeline.connection import ConnectionStore
from plugins.safeline.routes import register_routes
from plugins.safeline.service import SafeLineService


def setup(context):
    path = context.settings.safeline_state_file or str(Path(context.settings.data_dir or Path.cwd() / "data") / "safeline.sqlite3")
    store = ConnectionStore(path, context.settings.upstream)
    service = SafeLineService(store, context.transports.get("safeline"))
    register_routes(context, store, service)
    context.application.state.safeline_service = service

    async def shutdown():
        store.close()

    return PluginHandle(shutdown=shutdown)
