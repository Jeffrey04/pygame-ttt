import asyncio
import signal
from dataclasses import dataclass, replace
from enum import Enum, auto
from itertools import product
from typing import Any

import pygame
import structlog
from structlog.stdlib import BoundLogger

from ttt.core import (
    Application,
    ApplicationDataField,
    DeltaAdd,
    DeltaUpdate,
    Element,
    ShutdownHandler,
    SystemEvent,
    add_event_listener,
    add_event_listeners,
    main_loop,
    screen_update,
    state_get,
    state_merge,
)

PEN_WIDTH = 5
BOX_PADDING = 5
WIN_LINES = [
    # Rows
    {(0, 0), (0, 1), (0, 2)},
    {(1, 0), (1, 1), (1, 2)},
    {(2, 0), (2, 1), (2, 2)},
    # Columns
    {(0, 0), (1, 0), (2, 0)},
    {(0, 1), (1, 1), (2, 1)},
    {(0, 2), (1, 2), (2, 2)},
    # Diagonals
    {(0, 0), (1, 1), (2, 2)},
    {(0, 2), (1, 1), (2, 0)},
]


class Symbol(Enum):
    CROSS = auto()
    RING = auto()
    EMPTY = auto()


@dataclass(frozen=True)
class Box(Element):
    column: int = 0
    row: int = 0
    value: Symbol = Symbol.EMPTY


class GameState(Enum):
    INIT = auto()
    CROSS = auto()
    RING = auto()
    END = auto()
    TIE = auto()


class CustomEvent(Enum):
    RESET = pygame.event.custom_type()
    START = pygame.event.custom_type()
    SWITCH = pygame.event.custom_type()
    END = pygame.event.custom_type()
    UPDATEMAP = pygame.event.custom_type()
    JUDGE = pygame.event.custom_type()


@dataclass
class BoardMap:
    mapping: dict[tuple[int, int], Symbol]


async def box_draw(
    application: Application,
    row: int,
    column: int,
    size: int,
) -> Box:
    position = pygame.Rect(column * size, row * size, size, size)

    pygame.draw.rect(application.screen, (255, 255, 255), position)

    return Box(position=position, column=column, row=row)


async def handle_switch(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    await state_merge(target, "board", state=detail["to"])


async def handle_start(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    await state_merge(target, "board", state=detail["start"])


async def handle_init(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    for row, col in product(range(3), range(3)):
        element = await box_draw(target, row, col, 100)
        element = await add_event_listener(
            element,
            pygame.MOUSEBUTTONDOWN,
            handle_box_click,
        )
        await target.delta_data.put(DeltaAdd(ApplicationDataField.ELEMENTS, element))
        await screen_update(target, element)  # type: ignore

    await state_merge(target, "board", map=None)

    pygame.event.post(
        pygame.event.Event(CustomEvent.START.value, detail={"start": GameState.RING})
    )

async def handle_reset(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    await state_merge(target, "board", winner=None, state=GameState.END, board=None)

    for element in target.elements.values():
        delta = await box_redraw(target, (255, 255, 255), element, Symbol.EMPTY)  # type: ignore
        await target.delta_data.put(
            DeltaUpdate(ApplicationDataField.ELEMENTS, element, delta)
        )
        await screen_update(target, delta)

    pygame.event.post(
        pygame.event.Event(CustomEvent.START.value, detail={"start": GameState.RING})
    )


async def handle_box_click(
    _event: pygame.event.Event,
    target: Box,
    application: Application,
    logger: BoundLogger,
    **detail: Any,
) -> None:
    element = None

    match state_get(application, "board").get("state"):
        case GameState.INIT:
            await logger.aerror("Game is not started")

        case GameState.END:
            await logger.ainfo("END")
            pygame.event.post(pygame.event.Event(CustomEvent.RESET.value))

        case GameState.TIE:
            await logger.ainfo("TIE")
            pygame.event.post(pygame.event.Event(CustomEvent.RESET.value))

        case GameState.RING if target.value == Symbol.EMPTY:
            element = await box_update(
                application,
                target,
                Symbol.RING,
                GameState.CROSS,
            )

        case GameState.CROSS if target.value == Symbol.EMPTY:
            element = await box_update(
                application,
                target,
                Symbol.CROSS,
                GameState.RING,
            )

        case _:
            await logger.aerror("Invalid click")

    if element:
        pygame.event.post(pygame.event.Event(CustomEvent.UPDATEMAP.value))


async def box_update(
    application: Application, target: Box, symbol: Symbol, next_state: GameState
) -> Box:
    element = await box_redraw(
        application,
        (255, 255, 255),
        target,
        symbol,
    )
    await application.delta_data.put(
        DeltaUpdate(ApplicationDataField.ELEMENTS, target, element)
    )
    await screen_update(application, element)

    pygame.event.post(
        pygame.event.Event(CustomEvent.SWITCH.value, detail={"to": next_state})
    )

    return element


async def handle_update(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    await state_merge(
        target,
        "board",
        map=BoardMap(
            {
                (item.column, item.row): item.value
                for item in target.elements.values()
                if isinstance(item, Box)
            }
        ),
    )

    pygame.event.post(pygame.event.Event(CustomEvent.JUDGE.value))


async def handle_judge(
    _event: pygame.event.Event, target: Application, logger: BoundLogger, **detail: Any
):
    if not (bmap := state_get(target, "board").get("map")):
        return

    tiles, winner = (), None

    for symbol in (Symbol.RING, Symbol.CROSS):
        places = {coor for coor, item in bmap.mapping.items() if symbol == item}

        for line in WIN_LINES:
            if line.issubset(places):
                winner = symbol
                tiles = line
                break

        if winner:
            break

    if winner:
        await logger.ainfo("Winner is found", winner=winner)
        await state_merge(target, "board", winner=winner, state=GameState.END)

        for element in target.elements.values():
            if (element.column, element.row) in tiles:  # type: ignore
                delta = await box_redraw(target, (255, 255, 0), element, winner)  # type: ignore
                await target.delta_data.put(
                    DeltaUpdate(ApplicationDataField.ELEMENTS, element, delta)
                )
                await screen_update(
                    target,
                    delta,  # type: ignore
                )
    elif (
        len([symbol for _, symbol in bmap.mapping.items() if symbol == Symbol.EMPTY])
        == 0
    ):
        await logger.ainfo("End with a tie")
        await state_merge(target, "board", state=GameState.TIE)


async def box_redraw(
    application: Application,
    color: tuple[int, int, int],
    box: Box,
    value: Symbol,
) -> Box:
    assert isinstance(box.position, pygame.Rect)

    pygame.draw.rect(application.screen, color, box.position)

    match value:
        case Symbol.RING:
            pygame.draw.ellipse(
                application.screen,
                (0, 0, 0),
                pygame.Rect(
                    box.position.left + BOX_PADDING,
                    box.position.top + BOX_PADDING,
                    box.position.width - (BOX_PADDING * 2),
                    box.position.height - (BOX_PADDING * 2),
                ),
                PEN_WIDTH,
            )

        case Symbol.CROSS:
            pygame.draw.line(
                application.screen,
                (0, 0, 0),
                (box.position.left + BOX_PADDING, box.position.top + BOX_PADDING),
                (box.position.right - BOX_PADDING, box.position.bottom - BOX_PADDING),
                PEN_WIDTH,
            )
            pygame.draw.line(
                application.screen,
                (0, 0, 0),
                (box.position.right - BOX_PADDING, box.position.top + BOX_PADDING),
                (box.position.left + BOX_PADDING, box.position.bottom - BOX_PADDING),
                PEN_WIDTH,
            )

    return replace(box, value=value)


async def setup(logger: BoundLogger) -> Application:
    application = Application(
        screen=pygame.display.set_mode((300, 300), pygame.NOFRAME)
    )
    await add_event_listeners(
        application,
        (
            (SystemEvent.INIT.value, handle_init),
            (CustomEvent.RESET.value, handle_reset),
            (CustomEvent.SWITCH.value, handle_switch),
            (CustomEvent.START.value, handle_start),
            (CustomEvent.UPDATEMAP.value, handle_update),
            (CustomEvent.JUDGE.value, handle_judge),
        ),
    )

    assert isinstance(application, Application)

    shutdown_handler = ShutdownHandler(application.exit_event, logger)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(s, shutdown_handler)

    return application  # type: ignore


async def run() -> None:
    logger = structlog.get_logger().bind(module=__name__)

    await main_loop(setup(logger))
