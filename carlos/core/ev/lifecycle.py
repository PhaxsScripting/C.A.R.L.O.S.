"""Diagnosable bounded asynchronous shutdown, without killing unrelated PIDs."""

import asyncio
import time


class ShutdownBlocked(RuntimeError):
    pass


async def shutdown_tasks(name, tasks, logger, timeout=10.0):
    tasks = tuple(tasks)
    for task in tasks:
        task.cancel()

    async def drain():
        await asyncio.gather(*tasks, return_exceptions=True)

    return await shutdown_step(name, drain, logger, timeout=timeout)


async def shutdown_step(name, operation, logger, timeout=10.0, cancel_grace=1.0):
    started = time.monotonic()
    logger.info("Shutdown stage started", extra={"fields": {"stage": name}})
    task = asyncio.create_task(operation(), name="shutdown:" + name)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            frames = [f.f_code.co_name for f in task.get_stack(limit=8)]
            logger.warning(
                "Shutdown stage timed out", extra={"fields": {"stage": name, "frames": frames}}
            )
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=cancel_grace)
            if not done:
                # Do not announce clean shutdown/release ownership while a
                # cancellation-resistant cleanup task can still change state.
                raise ShutdownBlocked(f"Shutdown stage {name} ignored cancellation")
            await asyncio.gather(task, return_exceptions=True)
            return False
        try:
            task.result()
        except asyncio.CancelledError:
            return False
        except Exception as error:
            logger.warning(
                "Shutdown stage failed",
                extra={"fields": {"stage": name, "error_type": type(error).__name__}},
            )
            return False
        return True
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task}, timeout=cancel_grace)
        raise
    finally:
        logger.info(
            "Shutdown stage finished",
            extra={
                "fields": {
                    "stage": name,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            },
        )
