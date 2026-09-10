from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin.app import app
from bot.loader import dp
from bot.main import on_startup, run_bot, setup_dispatcher
from config import settings
from database import init_db

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])


async def main() -> None:
    await init_db()
    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    tasks = [asyncio.create_task(server.serve(), name="web")]
    token_ok = settings.bot_token and ":" in settings.bot_token and not settings.bot_token.startswith("123456")
    if token_ok:
        tasks.append(asyncio.create_task(run_bot(), name="bot"))
    else:
        setup_dispatcher()
        await on_startup()
        logging.warning("BOT_TOKEN не задан — запущен только веб-админ на http://127.0.0.1:%s", settings.web_port)

    # uvicorn's Server and aiogram's Dispatcher each want their own SIGTERM
    # handler; asyncio only allows one per signal, so whichever registers last
    # would silently swallow it. Route both from a single handler instead,
    # otherwise `systemctl restart` hangs until systemd's SIGKILL timeout.
    def _shutdown(sig: signal.Signals) -> None:
        logging.warning("Получен сигнал %s — останавливаюсь", sig.name)
        server.should_exit = True
        if token_ok:
            asyncio.create_task(dp.stop_polling())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown, sig)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
