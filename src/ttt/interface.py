import asyncio
import signal
from abc import ABC
from collections.abc import Awaitable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from itertools import product
from typing import Any, Callable

import pygame
import structlog
from structlog.stdlib import BoundLogger


@dataclass
class ShutdownHandler:
    exit_event: asyncio.Event
    logger: BoundLogger

    def __call__(self) -> None:
        self.logger.info("Sending exit event")
        self.exit_event.set()


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
class Eventable:
    events: tuple[Event, ...] = tuple()


@dataclass(frozen=True)
class Element(Eventable):
    # the bounding box
    position: pygame.Rect | None = None

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
class DeltaOperation(ABC):
    item: Any


@dataclass
class DeltaAdd(DeltaOperation):
    pass


@dataclass
class DeltaUpdate(DeltaOperation):
    new: Any


@dataclass
class DeltaDelete(DeltaOperation):
    pass


@dataclass(frozen=True)
class Application(Eventable):
    screen: pygame.Surface = pygame.Surface((0, 0))
    clock: pygame.time.Clock = pygame.time.Clock()

    elements: tuple[Element, ...] = tuple()

    state: GameState = GameState.INIT

    screen_update: asyncio.Queue[Element] = asyncio.Queue()
    state_update: asyncio.Queue[GameState] = asyncio.Queue()
    event_delta: asyncio.Queue[DeltaOperation] = asyncio.Queue()
    element_delta: asyncio.Queue[DeltaOperation] = asyncio.Queue()
    exit_event: asyncio.Event = asyncio.Event()


def dispatch_application_handler(
    application: Application,
    event: pygame.event.Event,
    checker: Callable[[Application, pygame.event.Event], bool] | None = None,
):
    for ev in application.events:
        if not ev.kind == event.type:
            continue
        elif checker is None or checker(application, event):
            asyncio.create_task(ev.handler(event, application, **event.detail))


def dispatch_element_handler(
    elements: tuple[Element, ...],
    event: pygame.event.Event,
    checker: Callable[[Element, pygame.event.Event], bool] | None = None,
    **kwargs: Any,
):
    for element in elements:
        for ev in element.events:
            if not ev.kind == event.type:
                continue
            elif checker is None or checker(element, event):
                asyncio.create_task(
                    ev.handler(
                        event,
                        element,
                        **dict(
                            event.detail if hasattr(event, "detail") else {}, **kwargs
                        ),
                    )
                )


def check_is_collide(element: Element, event: pygame.event.Event) -> bool:
    assert isinstance(element.position, pygame.Rect)

    mouse_x, mouse_y = event.pos

    return element.position.collidepoint(mouse_x, mouse_y)


async def events_dispatch(
    application: Application,
    events: Sequence[pygame.event.Event],
    logger: BoundLogger,
) -> None:
    for event in events:
        match event.type:
            case pygame.QUIT:
                application.exit_event.set()

            case pygame.MOUSEBUTTONDOWN:
                dispatch_application_handler(application, event)
                dispatch_element_handler(
                    application.elements,
                    event,
                    check_is_collide,
                    application=application,
                    logger=logger,
                )

            case (
                CustomEvent.START.value
                | CustomEvent.SWITCH.value
                | CustomEvent.RESET.value
            ):
                dispatch_application_handler(application, event)
                dispatch_element_handler(application.elements, event)

            # case _:
            #    await logger.aerror("Unhandled event", pygame_event=event)


async def draw_box(
    application: Application,
    row: int,
    column: int,
    size: int,
) -> Box:
    position = pygame.Rect(column * size, row * size, size, size)

    pygame.draw.rect(application.screen, (255, 255, 255), position)

    return Box(position=position, column=column, row=row)


async def redraw_box(
    application: Application, box: Box, value: Symbol, logger: BoundLogger
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


async def handle_switch(_event: pygame.event.Event, target: Application, **detail: Any):
    await target.state_update.put(detail["to"])


async def handle_start(_event: pygame.event.Event, target: Application, **detail: Any):
    await target.state_update.put(detail["start"])


async def handle_init(_event: pygame.event.Event, target: Application, **detail: Any):
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


async def setup(logger: BoundLogger) -> Application:
    application = Application(
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

    assert isinstance(application, Application)

    shutdown_handler = ShutdownHandler(application.exit_event, logger)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(s, shutdown_handler)

    return application  # type: ignore


async def application_refresh(application: Application) -> Application:
    return replace(
        application,
        elements=delta_queue_process(application.elements, application.element_delta),
        state=state_queue_process(application.state, application.state_update),
        events=delta_queue_process(application.events, application.event_delta),
    )


def state_queue_process(
    current: GameState, state_queue: asyncio.Queue[GameState]
) -> GameState:
    with suppress(asyncio.queues.QueueEmpty):
        result = current

        while state_new := state_queue.get_nowait():
            result = state_new

    return result


def delta_queue_process(
    target_list: tuple[Any, ...], delta_queue: asyncio.Queue[Any]
) -> tuple[Any, ...]:
    with suppress(asyncio.queues.QueueEmpty):
        result = tuple(element for element in target_list)

        while delta := delta_queue.get_nowait():
            match delta:
                case DeltaAdd():
                    result += (delta.item,)

                case DeltaUpdate():
                    result = tuple(
                        delta.new if element == delta.item else element
                        for element in target_list
                    )

                case DeltaDelete():
                    result = tuple(
                        element for element in target_list if not element == delta.item
                    )

    return result


async def coroutine_loop(
    func: Callable[..., Awaitable[Any | None]], *args: Any
) -> None:
    with suppress(asyncio.CancelledError):
        while True:
            if returns := await func(*args):
                args = returns

            await asyncio.sleep(0)


async def display_update(
    screen_update: asyncio.Queue, clock: pygame.time.Clock
) -> None:
    clock.tick(60)

    with suppress(asyncio.queues.QueueEmpty):
        while element := screen_update.get_nowait():
            pygame.display.update(element.position)


async def events_process(
    application: Application, logger: BoundLogger
) -> tuple[Application, BoundLogger]:
    application = await application_refresh(application)

    await events_dispatch(
        application,
        pygame.event.get(),
        logger,
    )

    return application, logger


async def run() -> None:
    pygame.init()

    logger = structlog.get_logger().bind(module=__name__)

    await logger.ainfo("Initializing interface")
    application = await setup(logger)

    pygame.event.post(
        pygame.event.Event(CustomEvent.RESET.value, detail={"logger": logger})
    )

    tasks = []

    await logger.ainfo("Setting up display update loop")
    tasks.append(
        asyncio.create_task(
            coroutine_loop(
                display_update,
                application.screen_update,
                application.clock,
            )
        )
    )

    await logger.ainfo("Setting up event dispatching loop")
    tasks.append(
        asyncio.create_task(coroutine_loop(events_process, application, logger))
    )

    await application.exit_event.wait()

    await logger.ainfo("Shutting down background tasks")
    for task in tasks:
        task.cancel()

    await logger.ainfo("Exiting interface")
    pygame.quit()


async def add_event_listener(target: Eventable, kind: int, handler) -> Eventable:
    event = Event(kind, handler)
    result = target

    match target:
        case Application():
            await target.event_delta.put(DeltaAdd(event))

        case Element():
            result = replace(target, events=target.events + (event,))

        case _:
            raise Exception("Unhandled event registration")

    return result


async def screen_update(application: Application, element: Element) -> Application:
    await application.screen_update.put(element)

    return application
