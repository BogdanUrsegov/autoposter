"""Runtime compatibility helpers loaded automatically by Python's site module."""

import asyncio
import builtins
import sys


def _ensure_send_worker(account_id: int):
    """Create/reuse the per-account userbot send queue and worker."""
    module = sys.modules.get("bot.services.userbot")
    if module is None:
        raise RuntimeError("bot.services.userbot is not loaded")

    queues = module._send_queues
    workers = module._send_workers
    worker = module._send_worker

    queue = queues.get(account_id)
    if queue is None:
        queue = asyncio.PriorityQueue()
        queues[account_id] = queue

    task = workers.get(account_id)
    if task is None or task.done():
        task = asyncio.create_task(
            worker(account_id),
            name=f"userbot-send-worker-{account_id}",
        )
        workers[account_id] = task

    return queue


# userbot.py expects this helper as a module-global. Defining it in builtins
# keeps the fix minimal when deploying a version where the helper was omitted.
builtins._ensure_send_worker = _ensure_send_worker
