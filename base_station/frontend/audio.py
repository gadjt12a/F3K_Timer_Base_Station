"""F3K Base Station — Audio cue stub (real mpg123 implementation is Task 8)."""

import logging

log = logging.getLogger("f3k")


async def play_cue(name: str) -> None:
    log.info("[AUDIO] %s", name)
