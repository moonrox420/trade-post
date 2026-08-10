import asyncio
import json
import logging
from datetime import datetime
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    def register(self, task_id: str, coro):
        task = asyncio.create_task(coro)
        self.tasks[task_id] = task
        task.add_done_callback(lambda t: self.tasks.pop(task_id, None))
        return task

    async def shutdown(self):
        logger.info(f"Shutting down {len(self.tasks)} managed tasks...")
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


class ConnectionManager:
    def __init__(self):
        self.active: dict[str, WebSocket] = {}
        self.subscriptions: dict[str, set[str]] = {}

    async def connect(self, websocket: WebSocket, sid: str):
        await websocket.accept()
        self.active[sid] = websocket
        self.subscriptions[sid] = set()

    def disconnect(self, sid: str):
        self.active.pop(sid, None)
        self.subscriptions.pop(sid, None)

    async def subscribe(self, sid: str, symbols: list[str]):
        if sid in self.subscriptions:
            self.subscriptions[sid].update(symbols)

    async def unsubscribe(self, sid: str, symbols: list[str]):
        if sid in self.subscriptions:
            for s in symbols:
                self.subscriptions[sid].discard(s)

    async def broadcast(self, message: dict):
        import pandas as pd

        def json_serial(obj):
            if isinstance(obj, (datetime, pd.Timestamp)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        payload = json.dumps(message, default=json_serial)
        msg_type = message.get("type")
        msg_symbol = message.get("data", {}).get("symbol")

        for sid, ws in list(self.active.items()):
            try:
                if msg_type == "market_snapshot" and msg_symbol:
                    if msg_symbol not in self.subscriptions.get(sid, set()):
                        continue
                await ws.send_text(payload)
            except Exception:
                pass


manager = ConnectionManager()
