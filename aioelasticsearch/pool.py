import asyncio
import collections
import logging
import random

from elasticsearch.connection_pool import ConnectionSelector

from .compat import create_future
from .exceptions import ImproperlyConfigured

logger = logging.getLogger('elasticsearch')


class RoundRobinSelector(ConnectionSelector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._current = 0

    def select(self, connections):
        self._current += 1
        if self._current >= len(connections):
            self._current = 0
        return connections[self._current]


class AIOHttpConnectionPool:

    def __init__(
        self,
        connections,
        dead_timeout=60,
        timeout_cutoff=5,
        selector_class=RoundRobinSelector,
        randomize_hosts=True,
        *,
        loop,
        **kwargs
    ):
        self._dead_timeout = dead_timeout
        self.timeout_cutoff = timeout_cutoff
        self.connection_opts = connections
        self.connections = [c for (c, _) in connections]
        self.orig_connections = set(self.connections)
        self.dead = asyncio.PriorityQueue(len(self.connections), loop=loop)
        self.dead_count = collections.Counter()

        self.loop = loop

        if randomize_hosts:
            random.shuffle(self.connections)

        self.selector = selector_class(dict(connections))

    def dead_timeout(self, dead_count):
        exponent = min(dead_count - 1, self.timeout_cutoff)
        return self._dead_timeout * 2 ** exponent

    def mark_dead(self, connection):
        now = self.loop.time()

        try:
            self.connections.remove(connection)
        except ValueError:
            # connection not alive or marked already, ignore
            return
        else:
            self.dead_count[connection] += 1
            dead_count = self.dead_count[connection]

            timeout = self.dead_timeout(dead_count)

            # it is impossible to raise QueueEmpty here
            self.dead.put_nowait((now + timeout, connection))

            logger.warning(
                'Connection %r has failed for %i times in a row, '
                'putting on %i second timeout.',
                connection, dead_count, timeout,
            )

    def mark_live(self, connection):
        try:
            del self.dead_count[connection]
        except KeyError:
            # possible due to race condition
            pass

    def resurrect(self, force=False):
        if self.dead.empty():
            if force:
                return random.choice(self.orig_connections)
            return

        timeout, connection = self.dead.get_nowait()

        if not force and timeout > self.loop.time():
            # return it back if not eligible and not forced
            self.dead.put_nowait((timeout, connection))
            return

        # either we were forced or the connection is elligible to be retried
        self.connections.append(connection)

        logger.info(
            'Resurrecting connection %r (force=%s).',
            connection, force,
        )

        return connection

    def get_connection(self):
        self.resurrect()

        if not self.connections:
            return self.resurrect(force=True)

        if len(self.connections) > 1:
            return self.selector.select(self.connections)

        return self.connections[0]

    def close(self, skip=None):
        if skip is None:
            skip = set()

        coros = [
            connection.close() for connection in
            self.orig_connections - skip
        ]

        return asyncio.gather(*coros, loop=self.loop)


class DummyConnectionPool(AIOHttpConnectionPool):

    def __init__(self, connections, *, loop, **kwargs):
        if len(connections) != 1:
            raise ImproperlyConfigured(
                'DummyConnectionPool needs exactly one connection defined.',
            )

        self.loop = loop

        self.connection_opts = connections
        self.connection = connections[0][0]
        self.connections = (self.connection, )
        self.orig_connections = set(self.connections)

    def get_connection(self):
        return self.connection

    def close(self, skip=None):
        if skip is None:
            skip = set()

        if self.connection in skip:
            fut = create_future(loop=self.loop)
            fut.set_result(None)
            return fut

        return self.connection.close()

    def mark_live(self, connection):
        pass

    def mark_dead(self, connection):
        pass

    def resurrect(self, force=False):
        pass
