"""Channel transformations."""

# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/02_transforms.ipynb.

# %% auto 0
__all__ = ['Transform', 'ParDo', 'as_transform', 'Event', 'Streamable', 'use_sink', 'cur_sink', 'tfn']

# %% ../nbs/02_transforms.ipynb 3
import abc
import asyncio
import time
import uuid
import inspect
import contextlib
import contextvars
import dataclasses
from typing import Any, Callable, ParamSpec, Protocol, Generic, TypeVar

from fastcore.basics import patch

import fastagent_hacking.streams as sx
import fastagent_hacking.channels as cx

# %% ../nbs/02_transforms.ipynb 7
_I = TypeVar("I")
_O = TypeVar("O")


class Transform(abc.ABC, Generic[_I, _O]):

    @abc.abstractmethod
    def __call__(self, chan: cx.Channel[_I]) -> cx.Channel[_O]:
        """Transforms the input channel into an output channel."""

# %% ../nbs/02_transforms.ipynb 8
# FIXME: Move this to a separate module.
def _print_task_errors(task: asyncio.Task):
    if task.exception():
        task.print_stack()
        print(f"Task failed with exception: {task.exception()}")

# %% ../nbs/02_transforms.ipynb 9
class ParDo(Transform[_I, _O]):
    """Processes each element in the input channel using a user-defined function."""

    def __init__(self, fn):  # FIXME: type hint
        self._fn = fn

    def __call__(self, chan: cx.Channel[_I]) -> cx.Channel[_O]:
        writer = sx.InMemStreamWriter()

        async def proc(chan):
            try:
                async for p in chan:
                    assert isinstance(p, cx.Packet)
                    if self._is_passthrough(p):
                        await writer.put(p)
                        continue
                    s = self._proc_packet(p)
                    await writer.put(s)
            finally:
                await writer.shutdown()

        asyncio.create_task(proc(chan)).add_done_callback(_print_task_errors)

        return cx.as_chan(
            sx.flatten(writer.readonly()),
            chan.elm_type,
        )

    def _proc_packet(self, p: cx.Packet[_I]) -> sx.Stream[cx.Packet[_O]]:
        assert p.packet_type == cx.PacketType.MAIN
        fn = sx.streamify(self._fn)
        s = fn(p.payload)
        return sx.map(
            lambda x: cx.Packet(
                payload=x,
                packet_type=cx.PacketType.MAIN,
                packet_id=str(uuid.uuid4()),
                parent_packet_id=p.packet_id,
                stamp="0",  # FIXME: Use real value.
                created_at=time.time(),
            ),
            s,
        )

    def _is_passthrough(self, p: cx.Packet) -> bool:
        return p.packet_type != cx.PacketType.MAIN

# %% ../nbs/02_transforms.ipynb 10
def as_transform(fn: Callable | Transform) -> Transform:
    """Converts a function of a single argument into a Transform object."""
    if isinstance(fn, Transform):
        return fn

    return ParDo(fn)

# %% ../nbs/02_transforms.ipynb 11
@patch
def __or__(
    self: Transform,
    other,
) -> Transform:
    t1, t2 = self, as_transform(other)

    class ComposedTransform(Transform):

        def __call__(self, chan: cx.Channel) -> cx.Channel:
            return t2(t1(chan))

    return ComposedTransform()


@patch
def __ror__(
    self: Transform,
    other,
) -> Transform:
    t2, t1 = self, as_transform(other)

    class ComposedTransform(Transform):

        def __call__(self, chan: cx.Channel) -> cx.Channel:
            return t2(t1(chan))

    return ComposedTransform()

# %% ../nbs/02_transforms.ipynb 22
@dataclasses.dataclass(frozen=True)
class Event:
    payload: Any
    src: str = ""

# %% ../nbs/02_transforms.ipynb 23
_R = TypeVar("_R")
_P = ParamSpec("_P")


class Streamable(Protocol[_P, _R]):

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R: ...

    def stream(self, *args: _P.args, **kwargs: _P.kwargs) -> cx.Channel[Event]: ...

    def __or__(self, other) -> Transform: ...

# %% ../nbs/02_transforms.ipynb 24
_sink_ctxvar = contextvars.ContextVar("_sink_contextvar", default=None)


@contextlib.contextmanager
def use_sink(sink: sx.StreamWriter):
    try:
        tok = _sink_ctxvar.set(sink)
        yield sink
    finally:
        _sink_ctxvar.reset(tok)


def cur_sink() -> sx.StreamWriter | None:
    return _sink_ctxvar.get()

# %% ../nbs/02_transforms.ipynb 25
import functools
import inspect
import types
from typing import Awaitable

# FIXME How to improve the type hinting for decorated @tfn functions? (e.g., keep their signature).


def tfn(fn: Callable[..., Awaitable[_R]]) -> Streamable:
    assert not isinstance(
        fn, types.MethodType
    ), "tfn can only be used with functions, not methods"

    assert asyncio.iscoroutinefunction(fn) or inspect.isasyncgenfunction(
        fn
    ), "tfn can only be used with async functions or async generators"

    class _S(Streamable):

        # TODO: Factor out the common code between __call__s.
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def __call__(self, *args, **kwargs):
                """Handles async generators"""
                sink = kwargs.pop("sink", None)
                if not sink:
                    sink = cur_sink()

                if "sink" in inspect.signature(fn).parameters:
                    kwargs["sink"] = sink

                async for e in fn(*args, **kwargs):
                    yield e  # Async generator case

        else:

            @functools.wraps(fn)
            async def __call__(self, *args, **kwargs):
                """Handles normal async functions"""
                sink = kwargs.pop("sink", None)
                if not sink:
                    sink = cur_sink()

                if "sink" in inspect.signature(fn).parameters:
                    kwargs["sink"] = sink

                return await fn(*args, **kwargs)  # Normal async function case

        def stream(self, *args, return_value: bool = False, **kwargs):
            """Returns a streamable version of the function."""
            sink = sx.InMemStreamWriter()
            with use_sink(sink):

                async def target():
                    nonlocal sink
                    try:
                        result = await self(
                            *args, **kwargs, sink=sink
                        )  # FIXME Should we overwrite chan if already passed?
                        if return_value:
                            await sink.put(result)
                    finally:
                        await sink.shutdown()

                # TODO: We probably need a task cleanup.
                asyncio.create_task(target()).add_done_callback(_print_task_errors)
                return sink.readonly()

        def __or__(self, other) -> Transform:
            t1, t2 = as_transform(self), as_transform(other)
            return t1 | t2

        def __ror__(self, other) -> Transform:
            t2, t1 = as_transform(self), as_transform(other)
            return t1 | t2

    wrapped = _S()
    if asyncio.iscoroutinefunction(fn):
        inspect.markcoroutinefunction(wrapped)

    return wrapped
