import asyncio
from abc import ABC
from collections.abc import Awaitable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
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


class SystemEvent(Enum):
    INIT = pygame.event.custom_type()
    EXIT = pygame.event.custom_type()


class ApplicationDataField(Enum):
    STATE = auto()
    ELEMENTS = auto()
    EVENTS = auto()


@dataclass
class DeltaOperation(ABC):
    field: ApplicationDataField
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


@dataclass
class DeltaReplace(DeltaOperation):
    pass


@dataclass(frozen=True)
class Application(Eventable):
    screen: pygame.Surface = pygame.Surface((0, 0))
    state: Any = None
    clock: pygame.time.Clock = pygame.time.Clock()

    elements: tuple[Element, ...] = tuple()

    delta_data: asyncio.Queue[DeltaOperation] = asyncio.Queue()
    delta_screen: asyncio.Queue[Element] = asyncio.Queue()
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

            case custom if custom >= pygame.USEREVENT:
                dispatch_application_handler(application, event)
                dispatch_element_handler(application.elements, event)

            # case _:
            #    await logger.aerror("Unhandled event", pygame_event=event)


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

    updates = []

    with suppress(asyncio.queues.QueueEmpty):
        while element := screen_update.get_nowait():
            updates.append(element.position)

    pygame.display.update(updates)


def application_get_field(application, field: ApplicationDataField) -> str:
    result = None

    match field:
        case ApplicationDataField.EVENTS:
            result = "events"

        case ApplicationDataField.ELEMENTS:
            result = "elements"

        case ApplicationDataField.STATE:
            result = "state"

        case _:
            raise Exception("Wrong data field")

    assert hasattr(application, result)

    return result


async def application_refresh(application: Application) -> Application:
    result = application

    with suppress(asyncio.queues.QueueEmpty):
        while delta := application.delta_data.get_nowait():
            field = application_get_field(application, delta.field)

            match delta:
                case DeltaAdd():
                    result = replace(
                        result,
                        **{field: getattr(result, field) + (delta.item,)},
                    )

                case DeltaUpdate():
                    result = replace(
                        result,
                        **{
                            field: tuple(
                                delta.new if item == delta.item else item
                                for item in getattr(result, field)
                            )
                        },
                    )

                case DeltaDelete():
                    result = replace(
                        result,
                        **{
                            field: tuple(
                                item
                                for item in getattr(result, field)
                                if not item == delta.item
                            )
                        },
                    )

                case DeltaReplace():
                    result = replace(result, **{field: delta.item})

    return result


async def events_process(
    application: Application,
    logger: BoundLogger,
) -> tuple[Application, BoundLogger]:
    application = await application_refresh(application)

    await events_dispatch(
        application,
        pygame.event.get(),
        logger,
    )

    return application, logger


async def main_loop(
    application_setup: Awaitable[Application],
) -> None:
    pygame.init()

    logger = structlog.get_logger().bind(module=__name__)

    await logger.ainfo("Initializing interface")
    application = await application_setup

    pygame.event.post(
        pygame.event.Event(SystemEvent.INIT.value, detail={"logger": logger})
    )

    tasks = []

    await logger.ainfo("Setting up display update loop")
    tasks.append(
        asyncio.create_task(
            coroutine_loop(
                display_update,
                application.delta_screen,
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


async def add_event_listener(
    target: Eventable, kind: int, handler: Callable[..., Coroutine[None, None, None]]
) -> Eventable:
    event = Event(kind, handler)
    result = target

    match target:
        case Application():
            await target.delta_data.put(DeltaAdd(ApplicationDataField.EVENTS, event))

        case Element():
            result = replace(target, events=target.events + (event,))

        case _:
            raise Exception("Unhandled event registration")

    return result


async def screen_update(application: Application, element: Element) -> Application:
    await application.delta_screen.put(element)

    return application
