import asyncio
import signal
from abc import ABC
from collections.abc import Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from itertools import product
from typing import Callable

import pygame
import structlog
from structlog.stdlib import BoundLogger


class Symbol(Enum):
    CROSS = auto()
    RING = auto()
    EMPTY = auto()


class GameState(Enum):
    INIT = auto()
    CROSS = auto()
    RING = auto()
    WINNER = auto()
    TIE = auto()


PEN_WIDTH = 5
BOX_PADDING = 5


@dataclass
class Event:
    kind: int
    handler: Callable[..., Coroutine[None, None, None]]


@dataclass(frozen=True)
class Element:
    # the bounding box
    position: pygame.Rect

    events: tuple[Event, ...] = tuple()


@dataclass(frozen=True)
class Box(Element):
    column: int = 0
    row: int = 0
    value: Symbol = Symbol.EMPTY


class CustomEvent(Enum):
    RESET = pygame.event.custom_type()
    START = pygame.event.custom_type()
    SWITCH = pygame.event.custom_type()
    END = pygame.event.custom_type()


@dataclass
class ElementOperation(ABC):
    item: Element


@dataclass
class ElementAdd(ElementOperation):
    pass


@dataclass
class ElementUpdate(ElementOperation):
    new: Element


@dataclass
class ElementDelete(ElementOperation):
    pass


@dataclass(frozen=True)
class Application:
    screen: pygame.Surface
    clock: pygame.time.Clock

    elements: tuple[Element, ...] = tuple()

    state: GameState = GameState.INIT

    screen_update: asyncio.Queue[Element] = asyncio.Queue()
    state_update: asyncio.Queue[GameState] = asyncio.Queue()
    element_delta: asyncio.Queue[ElementOperation] = asyncio.Queue()
    exit_event: asyncio.Event = asyncio.Event()


async def handle_events(
    application: Application,
    events: Sequence[pygame.event.Event],
    logger: BoundLogger,
) -> None:
    for event in events:
        match event.type:
            case pygame.QUIT:
                application.exit_event.set()

            case pygame.MOUSEBUTTONDOWN:
                mouse_x, mouse_y = event.pos

                for element in application.elements:
                    for ev in element.events:
                        if not ev.kind == event.type:
                            continue

                        if element.position.collidepoint(mouse_x, mouse_y):
                            asyncio.create_task(
                                ev.handler(ev.kind, element, application, logger)
                            )

            case CustomEvent.START.value:
                asyncio.create_task(application.state_update.put(GameState.RING))

            case CustomEvent.SWITCH.value:
                asyncio.create_task(application.state_update.put(event.to))

            # case _:
            #    await logger.aerror("Unhandled event", pygame_event=event)


async def draw_box(
    application: Application,
    row: int,
    column: int,
    size: int,
    logger: BoundLogger,
) -> Box:
    logger.info("Drawing a box")

    position = pygame.Rect(column * size, row * size, size, size)

    pygame.draw.rect(application.screen, (255, 255, 255), position)

    element = Box(
        position,
        column=column,
        row=row,
    )
    await application.screen_update.put(element)

    return element


async def redraw_box(
    application: Application, box: Box, value: Symbol, logger: BoundLogger
) -> Box:
    # draw the base
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

    element = Box(box.position, box.events, box.column, box.row, value)

    await application.screen_update.put(element)

    return element


async def handle_box_click(
    _kind: int,
    target: Box,
    application: Application,
    logger: BoundLogger,
) -> None:
    match application.state:
        case GameState.INIT:
            await logger.aerror("Game is not started")

        case GameState.WINNER | GameState.TIE:
            pygame.event.post(pygame.event.Event(CustomEvent.RESET.value))

        case GameState.RING if target.value == Symbol.EMPTY:
            await application.element_delta.put(
                ElementUpdate(
                    target, await redraw_box(application, target, Symbol.RING, logger)
                )
            )

            pygame.event.post(
                pygame.event.Event(CustomEvent.SWITCH.value, to=GameState.CROSS)
            )

        case GameState.CROSS if target.value == Symbol.EMPTY:
            await application.element_delta.put(
                ElementUpdate(
                    target,
                    await redraw_box(application, target, Symbol.CROSS, logger),
                )
            )

            pygame.event.post(
                pygame.event.Event(CustomEvent.SWITCH.value, to=GameState.RING)
            )

        case _:
            await logger.aerror("Invalid click")


async def setup(logger: BoundLogger) -> Application:
    pygame.init()

    application = Application(
        pygame.display.set_mode((300, 300), pygame.NOFRAME),
        pygame.time.Clock(),
    )

    shutdown_handler = ShutdownHandler(application.exit_event, logger)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(s, shutdown_handler)

    return application


async def init(application: Application, logger: BoundLogger):
    logger.info("Drawing the initial grid")

    for row, col in product(range(3), range(3)):
        element = await draw_box(application, row, col, 100, logger)
        await application.element_delta.put(
            ElementAdd(
                Box(
                    element.position,
                    element.events + (Event(pygame.MOUSEBUTTONDOWN, handle_box_click),),
                    element.column,
                    element.row,
                    element.value,
                )
            )
        )


async def application_refresh(application: Application) -> Application:
    with suppress(asyncio.queues.QueueEmpty):
        elements = application.elements

        while delta := application.element_delta.get_nowait():
            match delta:
                case ElementAdd():
                    elements += (delta.item,)

                case ElementUpdate():
                    elements = tuple(
                        delta.new if element == delta.item else element
                        for element in elements
                    )

                case ElementDelete():
                    elements = tuple(
                        element for element in elements if not element == delta.item
                    )

    with suppress(asyncio.queues.QueueEmpty):
        state = application.state

        while state_new := application.state_update.get_nowait():
            state = state_new

    return Application(
        application.screen,
        application.clock,
        elements,
        state,
        application.screen_update,
        application.state_update,
        application.element_delta,
    )


@dataclass
class ShutdownHandler:
    exit_event: asyncio.Event
    logger: BoundLogger

    def __call__(self) -> None:
        self.logger.info("Sending exit event")
        self.exit_event.set()


async def run() -> None:
    logger = structlog.get_logger().bind(module=__name__)

    await logger.ainfo("Initializing interface")
    application = await setup(logger)

    await init(application, logger)

    pygame.event.post(pygame.event.Event(CustomEvent.START.value))

    await logger.ainfo(
        f"Display surface size reported by Pygame: {application.screen.get_size()}"
    )

    while not application.exit_event.is_set():
        application.clock.tick(10)

        asyncio.create_task(handle_events(application, pygame.event.get(), logger))

        application = await application_refresh(application)

        with suppress(asyncio.queues.QueueEmpty):
            while element := application.screen_update.get_nowait():
                pygame.display.update(element.position)

        await asyncio.sleep(0)

    await logger.ainfo("Exiting interface")
    pygame.quit()
