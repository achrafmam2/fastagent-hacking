"""Stream data structure."""

# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/00_streams.ipynb.

# %% auto 0
__all__ = ['StreamStatus', 'Stream', 'StreamWriter', 'InMemStreamWriter', 'tolist', 'of', 'concat', 'interleave', 'mix',
           'flatten', 'streamify', 'map', 'filter', 'zip', 'fork']

# %% ../nbs/00_streams.ipynb 3
import asyncio
import functools
import abc
import collections
import enum
from typing import (
    Any,
    Sequence,
    AsyncIterable,
    AsyncIterator,
    Iterable,
    TypeVar,
    Generic,
    Awaitable,
    Callable,
)

from fastcore.basics import patch

# %% ../nbs/00_streams.ipynb 7
_T = TypeVar("T")


class StreamStatus(enum.Enum):
    OK = enum.auto()
    SHUTDOWN = enum.auto()


class Stream(abc.ABC, AsyncIterator[_T]):

    @abc.abstractmethod
    async def next(
        self,
        with_status: bool = False,
    ) -> _T | None:
        pass

    async def __anext__(self) -> _T:
        e, status = await self.next(with_status=True)
        if status == StreamStatus.SHUTDOWN:
            raise StopAsyncIteration
        return e

    def __aiter__(self) -> AsyncIterator[_T]:
        return self

# %% ../nbs/00_streams.ipynb 9
class StreamWriter(abc.ABC, Generic[_T]):

    @abc.abstractmethod
    async def put(self, *items: _T):
        pass

    @abc.abstractmethod
    async def shutdown(self):
        pass

    @abc.abstractmethod
    def readonly(self) -> Stream[_T]:
        pass

# %% ../nbs/00_streams.ipynb 10
class InMemStreamWriter(StreamWriter[_T]):

    def __init__(self):
        self._q = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def put(self, *items: _T):
        async with self._lock:
            # Lock to ensure all elements are enqueued without being shutdown.
            try:
                for item in items:
                    await self._q.put(item)
            except asyncio.QueueShutDown:
                pass

    async def shutdown(self):
        async with self._lock:
            self._q.shutdown()

    def readonly(self) -> Stream[_T]:

        async def _next(
            w: InMemStreamWriter[_T], *, with_status: bool = False
        ) -> _T | None:
            item, status = None, StreamStatus.OK
            try:
                item = await w._q.get()
                w._q.task_done()
            except asyncio.QueueShutDown:
                item, status = None, StreamStatus.SHUTDOWN

            if with_status:
                return item, status
            return item

        class _S(Stream[_T]):
            next = lambda _, *args, **kwargs: _next(self, *args, **kwargs)

        return _S()

# %% ../nbs/00_streams.ipynb 17
async def tolist(s: Stream[_T]) -> list[_T]:
    return [e async for e in s]

# %% ../nbs/00_streams.ipynb 19
def of(*args: _T | AsyncIterable[_T] | Iterable[_T]) -> Stream[_T]:
    """Returns a Stream from the given source(s)."""

    class _FromIterableStream(Stream[_T]):

        def __init__(self, source: AsyncIterable[_T] | Iterable[_T]):
            if isinstance(source, AsyncIterable):
                self._iter = source.__aiter__()
            else:
                self._iter = self._to_aiter(source)

        async def next(
            self,
            with_status: bool = False,
        ) -> _T | None:
            try:
                item = await self._iter.__anext__()
                status = StreamStatus.OK
            except StopAsyncIteration:
                item, status = None, StreamStatus.SHUTDOWN

            if with_status:
                return item, status
            return item

        async def _to_aiter(self, iterable: Iterable[_T]) -> AsyncIterator[_T]:
            for item in iterable:
                # Simulate asynchronous behavior.
                await asyncio.sleep(0)
                yield item

    if len(args) == 1 and isinstance(args[0], (AsyncIterable, Iterable)):
        return _FromIterableStream(args[0])

    return _FromIterableStream(args)

# %% ../nbs/00_streams.ipynb 25
def concat(*streams: Stream[_T]) -> Stream[_T]:
    """Concatenates the given streams."""

    class _ConcatStream(Stream[_T]):

        def __init__(self):
            self._idx = 0

        async def next(
            self,
            with_status: bool = False,
        ) -> _T | None:
            while self._idx < len(streams):
                cur_stream = streams[self._idx]
                item, status = await cur_stream.next(with_status=True)
                if status == StreamStatus.OK:
                    if with_status:
                        return item, StreamStatus.OK
                    return item
                elif status == StreamStatus.SHUTDOWN:
                    self._idx += 1
                else:
                    assert False, f"Unexpected status: {status}"

            if with_status:
                return None, StreamStatus.SHUTDOWN
            return None

    return _ConcatStream()

# %% ../nbs/00_streams.ipynb 26
@patch
def __add__(
    self: Stream,
    other: Stream,
) -> Stream:
    return concat(self, other)

# %% ../nbs/00_streams.ipynb 32
def interleave(*streams: Stream[_T]) -> Stream[_T]:
    w = InMemStreamWriter()

    async def consume(s):
        nonlocal w
        async for e in s:
            await w.put(e)

    ts = [asyncio.create_task(consume(s)) for s in streams]

    async def cleanup():
        nonlocal ts
        await asyncio.gather(*ts)
        await w.shutdown()

    asyncio.create_task(cleanup())

    return w.readonly()

# %% ../nbs/00_streams.ipynb 33
def mix(*streams: Stream[_T]) -> Stream[_T]:
    return interleave(*streams)

# %% ../nbs/00_streams.ipynb 37
def flatten(s: Stream[_T | Stream[_T]]) -> Stream[_T]:
    """Flattens one level nested stream."""

    async def consume(s):
        async for x in s:
            if isinstance(x, Stream):
                async for y in x:
                    yield y
            else:
                yield x

    return of(consume(s))

# %% ../nbs/00_streams.ipynb 42
def streamify(
    func: Callable,
    *,
    return_shutdown_fn: bool = False,
) -> Callable:
    """Decorator to convert the output of a function to a stream.

    Handles both (a)sync functions, as well as (a)sync generators.

    Args:
      func: The function to be decorated.
      return_shutdown_fn: If True, calling the decorated function returns a tuple
        containing the stream and a function to close the stream generation. The
        shutdown function will try to cancel the decorated function if it's still running.
    """

    @functools.wraps(func)
    def wrapper(
        *args,
        **kwargs,
    ) -> Stream[_T] | tuple[Stream[_T] | Callable[[], None]]:
        sw = InMemStreamWriter()

        async def mk_stream():
            nonlocal sw
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                s = of(result)  # Handles also async and sync iterables.
                async for e in s:
                    await sw.put(e)
            finally:
                await sw.shutdown()

        # Write to the stream in the background.
        # FIXME: Handle errors otherwise they are silently ignored.
        t = asyncio.create_task(mk_stream())
        if return_shutdown_fn:
            # FIXME: Not sure but I think if the function is sync, the cancellation will not be immediate.
            #    If it's the case we may want to at least shutdown the stream soon.
            return sw.readonly(), t.cancel
        return sw.readonly()

    return wrapper

# %% ../nbs/00_streams.ipynb 51
def map(func, *streams) -> Stream[_T]:
    """Maps the given function over the given streams."""

    class _MappedStream(Stream[_T]):

        async def next(
            self,
            with_status: bool = False,
        ) -> _T | None:
            args = []
            for s in streams:
                e, status = await s.next(with_status=True)
                if status != StreamStatus.OK:
                    return None, status
                args.append(e)

            if asyncio.iscoroutinefunction(func):
                result = await func(*args)
            else:
                result = func(*args)

            if with_status:
                return result, StreamStatus.OK
            return result

    return _MappedStream()

# %% ../nbs/00_streams.ipynb 58
def filter(
    predicate: Callable[[_T], bool | Awaitable[bool]],
    stream: Stream[_T],
) -> Stream[_T]:
    """Filters the given stream using the given predicate.

    Args:
      predicate: A function or a coroutine that returns a boolean value.
        If True, the element is included in the output.
      streams: The streams to filter.
    """

    class _FilterdStream(Stream[_T]):

        async def next(
            self,
            with_status: bool = False,
        ) -> _T | None:
            e, status = await stream.next(with_status=True)
            if status != StreamStatus.OK:
                return None, status

            if asyncio.iscoroutinefunction(predicate):
                ok = await predicate(e)
            else:
                ok = predicate(e)

            if not ok:
                return await self.next(with_status=with_status)

            if with_status:
                return e, StreamStatus.OK
            return e

    return _FilterdStream()

# %% ../nbs/00_streams.ipynb 63
def zip(*streams: Stream) -> Stream[tuple[Any, ...]]:

    class _ZippedStream(Stream[tuple[_T]]):

        async def next(
            self,
            with_status: bool = False,
        ) -> tuple[_T] | None:
            items = []
            for s in streams:
                e, status = await s.next(with_status=True)
                if status != StreamStatus.OK:
                    return None, status
                items.append(e)

            if with_status:
                return tuple(items), StreamStatus.OK
            return tuple(items)

    return _ZippedStream()

# %% ../nbs/00_streams.ipynb 67
def fork(s: Stream[_T], n: int) -> Sequence[Stream[_T]]:
    """Make n copies of the given stream.

    The elements are copied by reference, so the streams share the same elements.
    The original stream must not be used after forking.
    """

    buffs = [collections.deque() for _ in range(n)]

    class _ForkedStream(Stream[_T]):

        def __init__(self, idx: int):
            self._idx = idx

        async def next(
            self,
            with_status: bool = False,
        ) -> _T | None:
            # Check if there is a buffered element.
            buff = buffs[self._idx]
            if buff:
                e = buff.popleft()
                if with_status:
                    return e, StreamStatus.OK
                return e

            # If not, get the next element from the source stream,
            # and buffer it for the other streams.
            e, status = await s.next(with_status=True)
            if status != StreamStatus.OK:
                return None, status

            for i, buff in enumerate(buffs):
                if i != self._idx:
                    buff.append(e)

            if with_status:
                return e, StreamStatus.OK
            return e

    return [_ForkedStream(i) for i in range(n)]
