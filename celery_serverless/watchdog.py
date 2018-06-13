# coding: utf-8
import os
import operator
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import backoff
from kombu import Connection
from kombu.transport import pyamqp

from celery_serverless.invoker import invoke as invoke_worker

logger = logging.getLogger(__name__)
logger.setLevel('DEBUG')

KOMBU_HEARTBEAT = 2


class Watchdog(object):
    def __init__(self, cache=None, name='', lock=None, watched=None):
        self._cache = cache or StrippedLocMemCache()
        self._name = name or 'celery_serverless:watchdog'
        self._lock = lock or threading.Lock()
        self._watched = watched

        # 0) Clear counters
        self.workers_started = 0
        self.workers_fulfilled = 0
        self.executor = ThreadPoolExecutor()

    @property
    def workers_started(self):
        return self._cache.get('%s:%s' % (self._name, 'workers_started'))

    @workers_started.setter
    def workers_started(self, value):
        self._cache.set('%s:%s' % (self._name, 'workers_started'), value)

    @property
    def workers_fulfilled(self):
        return self._cache.get('%s:%s' % (self._name, 'workers_fulfilled'))

    @workers_fulfilled.setter
    def workers_fulfilled(self, value):
        self._cache.set('%s:%s' % (self._name, 'workers_fulfilled'), value)

    @property
    def workers_not_served(self):
        return self.workers_started - self.workers_fulfilled

    @property
    def queue_length(self):
        if self._watched is None:
            logger.warning('Watchdog is watching None as queue. Fix it!')
            return 0
        return len(self._watched)

    def trigger_workers(self, how_much:int):
        # Hack to call parameterless 'invoke_worker' func -> lambda x: invoke_worker
        return len([i for i in self.executor.map(lambda x: invoke_worker, (None)*how_much)])

    @backoff.on_predicate(backoff.fibo)  # Will backoff until return True-ly val
    def _wait_starts(self, starts:int):
        return (self.workers_started >= starts)

    @backoff.on_predicate(backoff.fibo, predicate=operator.truth, max_time=20)
    def _wait_fulfillment(self):    # Stop backoff when 0 returned or max_time
        return self.workers_not_served

    def monitor(self):
        while self.queue_length:  # 1) See queue length N
            started = self.trigger_workers(self.queue_length)  # 2) Start N workers
            self._wait_starts(started)  # 3) Watch for N starts
            self._wait_fulfillment()  # 4) Wait then collect "Not Served" number

            # 5) Start "Not Served" number of workers.
            self.trigger_workers(self.workers_not_served)

        return self.workers_started  # How many had to be started to fulfill the queue?


class StrippedLocMemCache(object):
    # Stripped from Django's LocMemCache.
    # See: https://github.com/django/django/blob/master/django/core/cache/backends/locmem.py
    def __init__(self):
        self._cache = {}

    def get(self, key, default=None):
        return self._cache.get(key, default)

    def set(self, key, value):
        self._cache[key] = value

    def incr(self, key, delta=1):
        self._cache.setdefault(key, 0)
        self._cache[key] += delta
        return self.get(key)


# Queue length with ideas from ryanhiebert/hirefire
# See: https://github.com/ryanhiebert/hirefire/blob/67d57c8/hirefire/procs/celery.py#L239
def _AMQPChannel_size(self, queue):
        try:
            from librabbitmq import ChannelError
        except ImportError:
            from amqp.exceptions import ChannelError

        try:
            queue = self.queue_declare(queue, passive=True)
        except ChannelError:
            # The requested queue has not been created yet
            count = 0
        else:
            count = queue.message_count

        return count
pyamqp.Channel._size = _AMQPChannel_size


class KombuQueueLengther(object):
    def __init__(self, url, queue):
        self.connection = Connection(url, heartbeat=KOMBU_HEARTBEAT)
        self.queue = queue

    def __len__(self):
        return self.connection.channel()._size(self.queue)
