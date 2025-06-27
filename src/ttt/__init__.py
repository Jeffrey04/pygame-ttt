import asyncio

from ttt import game


def main() -> None:
    asyncio.run(game.run())
