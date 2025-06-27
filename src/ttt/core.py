import asyncio
from abc import ABC
from collections.abc import Awaitable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum
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

    screen_update: asyncio.Queue[Element] = asyncio.Queue()
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

            case custom if custom >= pygame.USEREVENT:
                dispatch_application_handler(application, event)
                dispatch_element_handler(application.elements, event)

            # case _:
            #    await logger.aerror("Unhandled event", pygame_event=event)


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

    updates = []

    with suppress(asyncio.queues.QueueEmpty):
        while element := screen_update.get_nowait():
            updates.append(element.position)

    pygame.display.update(updates)


async def events_process(
    application: Application,
    refresher: Callable[[Application], Awaitable[Application]],
    logger: BoundLogger,
) -> tuple[Application, Callable[[Application], Awaitable[Application]], BoundLogger]:
    application = await refresher(application)

    await events_dispatch(
        application,
        pygame.event.get(),
        logger,
    )

    return application, refresher, logger


async def main_loop(
    application_setup: Awaitable[Application],
    refresher: Callable[..., Awaitable[Application]],
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
                application.screen_update,
                application.clock,
            )
        )
    )

    await logger.ainfo("Setting up event dispatching loop")
    tasks.append(
        asyncio.create_task(
            coroutine_loop(events_process, application, refresher, logger)
        )
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
            await target.event_delta.put(DeltaAdd(event))

        case Element():
            result = replace(target, events=target.events + (event,))

        case _:
            raise Exception("Unhandled event registration")

    return result


async def screen_update(application: Application, element: Element) -> Application:
    await application.screen_update.put(element)

    return application
