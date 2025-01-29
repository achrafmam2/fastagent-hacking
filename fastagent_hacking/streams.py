"""Stream data structure."""

# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/00_streams.ipynb.

# %% auto 0
__all__ = ['StreamStatus', 'Stream', 'StreamWriter', 'InMemStreamWriter', 'tolist', 'of', 'concat', 'interleave', 'flatten',
           'streamify', 'map']

# %% ../nbs/00_streams.ipynb 3
import asyncio
import functools
import abc
import enum
from typing import AsyncIterable, AsyncIterator, Iterable, TypeVar, Generic

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

# %% ../nbs/00_streams.ipynb 16
async def tolist(s: Stream[_T]) -> list[_T]:
    xs = []
    async for x in s:
        xs.append(x)
    return xs

# %% ../nbs/00_streams.ipynb 18
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

# %% ../nbs/00_streams.ipynb 24
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

# %% ../nbs/00_streams.ipynb 29
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

# %% ../nbs/00_streams.ipynb 38
def streamify(func) -> Stream[_T]:

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Stream[_T]:
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
        asyncio.create_task(mk_stream())
        return sw.readonly()

    return wrapper

# %% ../nbs/00_streams.ipynb 46
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
