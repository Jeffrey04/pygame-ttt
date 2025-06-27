import asyncio
import signal
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from itertools import product
from typing import Any

import pygame
import structlog
from structlog.stdlib import BoundLogger

from ttt.core import (
    Application,
    DeltaAdd,
    DeltaUpdate,
    Element,
    ShutdownHandler,
    SystemEvent,
    add_event_listener,
    delta_queue_process,
    main_loop,
    screen_update,
)

PEN_WIDTH = 5
BOX_PADDING = 5


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
    WINNER = auto()
    TIE = auto()


class CustomEvent(Enum):
    RESET = pygame.event.custom_type()
    START = pygame.event.custom_type()
    SWITCH = pygame.event.custom_type()
    END = pygame.event.custom_type()


@dataclass(frozen=True)
class Game(Application):
    state: GameState = GameState.INIT

    state_update: asyncio.Queue[GameState] = asyncio.Queue()


async def draw_box(
    application: Game,
    row: int,
    column: int,
    size: int,
) -> Box:
    position = pygame.Rect(column * size, row * size, size, size)

    pygame.draw.rect(application.screen, (255, 255, 255), position)

    return Box(position=position, column=column, row=row)


async def handle_switch(_event: pygame.event.Event, target: Game, **detail: Any):
    await target.state_update.put(detail["to"])


async def handle_start(_event: pygame.event.Event, target: Game, **detail: Any):
    await target.state_update.put(detail["start"])


async def handle_init(_event: pygame.event.Event, target: Game, **detail: Any):
    for row, col in product(range(3), range(3)):
        element = await draw_box(target, row, col, 100)
        element = await add_event_listener(
            element,
            pygame.MOUSEBUTTONDOWN,
            handle_box_click,
        )
        await target.element_delta.put(DeltaAdd(element))
        await screen_update(target, element)  # type: ignore

    pygame.event.post(
        pygame.event.Event(CustomEvent.START.value, detail={"start": GameState.RING})
    )


async def handle_box_click(
    _event: pygame.event.Event,
    target: Box,
    **detail: Any,
) -> None:
    match detail["application"].state:
        case GameState.INIT:
            await detail["logger"].aerror("Game is not started")

        case GameState.WINNER | GameState.TIE:
            pygame.event.post(pygame.event.Event(CustomEvent.RESET.value))

        case GameState.RING if target.value == Symbol.EMPTY:
            element = await redraw_box(
                detail["application"], target, Symbol.RING, detail["logger"]
            )
            await detail["application"].element_delta.put(DeltaUpdate(target, element))
            await screen_update(detail["application"], element)

            pygame.event.post(
                pygame.event.Event(
                    CustomEvent.SWITCH.value, detail={"to": GameState.CROSS}
                )
            )

        case GameState.CROSS if target.value == Symbol.EMPTY:
            element = await redraw_box(
                detail["application"], target, Symbol.CROSS, detail["logger"]
            )
            await detail["application"].element_delta.put(DeltaUpdate(target, element))
            await screen_update(detail["application"], element)

            pygame.event.post(
                pygame.event.Event(
                    CustomEvent.SWITCH.value, detail={"to": GameState.RING}
                )
            )

        case _:
            await detail["logger"].aerror("Invalid click")


async def redraw_box(
    application: Game, box: Box, value: Symbol, logger: BoundLogger
) -> Box:
    assert isinstance(box.position, pygame.Rect)

    pygame.draw.rect(application.screen, (255, 255, 255), box.position)

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


async def setup(logger: BoundLogger) -> Game:
    application = Game(
        screen=pygame.display.set_mode((300, 300), pygame.NOFRAME),
    )
    application = await add_event_listener(
        application, CustomEvent.SWITCH.value, handle_switch
    )
    application = await add_event_listener(
        application, CustomEvent.START.value, handle_start
    )
    application = await add_event_listener(
        application, CustomEvent.RESET.value, handle_init
    )
    application = await add_event_listener(
        application, SystemEvent.INIT.value, handle_init
    )

    assert isinstance(application, Game)

    shutdown_handler = ShutdownHandler(application.exit_event, logger)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(s, shutdown_handler)

    return application  # type: ignore


def state_queue_process(
    current: GameState, state_queue: asyncio.Queue[GameState]
) -> GameState:
    with suppress(asyncio.queues.QueueEmpty):
        result = current

        while state_new := state_queue.get_nowait():
            result = state_new

    return result


async def application_refresh(application: Game) -> Game:
    return replace(
        application,
        elements=delta_queue_process(application.elements, application.element_delta),
        state=state_queue_process(application.state, application.state_update),
        events=delta_queue_process(application.events, application.event_delta),
    )


async def run() -> None:
    logger = structlog.get_logger().bind(module=__name__)

    await main_loop(setup(logger), application_refresh)
