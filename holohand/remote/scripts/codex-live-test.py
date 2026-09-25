import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import CodexBridge


async def run():
    state = Path.home() / ".local/state/holohand-remote/codex-test"
    state.mkdir(exist_ok=True)

    async def emit(*args):
        pass

    bridge = CodexBridge(state, emit)
    try:
        thread = await bridge.create("local-test", ".cache/holohand-browser-tests")
        await bridge.prompt(
            "local-test",
            thread["id"],
            "Respond with exactly HOLOHAND REMOTE TEST OK. Do not use tools or edit any files.",
        )
        async with asyncio.timeout(90):
            while True:
                await asyncio.sleep(1)
                if bridge.threads[thread["id"]]["status"] in ("completed", "failed", "interrupted"):
                    final = [
                        e["params"]["item"].get("text")
                        for e in bridge.events
                        if e["method"] == "item/completed"
                        and e["params"].get("item", {}).get("type") == "agentMessage"
                    ]
                    print(
                        json.dumps(
                            {"status": bridge.threads[thread["id"]]["status"], "messages": final}
                        )
                    )
                    break
    finally:
        await bridge.close()


asyncio.run(run())
