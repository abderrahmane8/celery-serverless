#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tests for `celery_worker_serverless` package."""

import time
import uuid
import logging
import pytest
from pytest_shutil import env
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from click.testing import CliRunner

from celery_serverless import cli, watchdog
from celery_serverless import handler_worker, handler_watchdog

logger = logging.getLogger(__name__)


def test_command_line_interface():
    """Test the CLI."""
    runner = CliRunner()
    result = runner.invoke(cli.main)
    # assert result.exit_code == 0
    # assert 'celery_serverless.cli.main' in result.output
    # help_result = runner.invoke(cli.main, ['--help'])
    # assert help_result.exit_code == 0
    # assert '--help  Show this message and exit.' in help_result.output


def test_worker_handler_minimal_call():
    with env.set_env(CELERY_SERVERLESS_INTERCOM_URL='disabled'):
        response = handler_worker(None, None)
    assert response


def test_watchdog_handler_minimal_call():
    _env = dict(
        CELERY_SERVERLESS_QUEUE_URL='disabled',
        CELERY_SERVERLESS_LOCK_URL='dummy_threading://',
        CELERY_SERVERLESS_INTERCOM_URL='disabled',
    )
    with env.set_env(**_env):
        response = handler_watchdog(None, None)
    assert response


_needed_parameters = ['CELERY_SERVERLESS_LOCK_URL', 'CELERY_SERVERLESS_QUEUE_URL', 'CELERY_SERVERLESS_INTERCOM_URL']
@pytest.mark.parametrize('envname', _needed_parameters)
def test_watchdog_needs_envvar(envname):
    try:
        with env.unset_env([envname]):
            handler_watchdog(None, None)
    except AssertionError as err:
        assert 'envvar should be set' in str(err)
    else:
        raise RuntimeError('Had not raised an AssertionError')


@pytest.mark.timeout(30)
def test_watchdog_shutdown(monkeypatch):
    queue_url = 'redis://'
    _env = dict(
        CELERY_SERVERLESS_QUEUE_URL=queue_url,
        CELERY_SERVERLESS_LOCK_URL=queue_url,
        CELERY_SERVERLESS_INTERCOM_URL=queue_url,
    )

    redis = pytest.importorskip('redis')
    from redis.exceptions import ConnectionError

    conn = redis.StrictRedis.from_url(queue_url)
    try:
        conn.ping()
    except ConnectionError as err:
        pytest.skip('Redis server is not available: %s' % err)

    monkeypatch.setattr(
        'celery_serverless.watchdog.Watchdog.get_workers_count',
        lambda self: 1,
    )

    _shutdown_requested_responses = [False, False, False, True, 'success']
    monkeypatch.setattr(
        'celery_serverless.watchdog.Watchdog.is_shutdown_requested',
        lambda self: _shutdown_requested_responses.pop(0),
    )

    with env.set_env(**_env):
        response = handler_watchdog(None, None)

    assert _shutdown_requested_responses == ['success'], 'handler not stopped with `is_shutdown_requested() = True`'


@pytest.mark.timeout(30)
def test_watchdog_monitor_redis_queues(monkeypatch):
    queue_url = 'redis://'
    queue_name = 'celery'

    redis = pytest.importorskip('redis')
    from redis.exceptions import ConnectionError

    conn = redis.StrictRedis.from_url(queue_url)
    try:
        conn.ping()
    except ConnectionError as err:
        pytest.skip('Redis server is not available: %s' % err)

    # Redis is available.
    # Lets set it up before test the watchdog

    def _simulate_worker_invocation(*args, **kwargs):
        """
        Simulates a Worker invocation cycle via Redis keys changes
        """
        ## Expects kwargs{} maybe containing:
        # 'data': {
        #     'worker_id': '2a3dc6a4-78a7-11e8-86e7-4c32758cbd8b',
        #     'worker_trigger_time': 1529951999.836363,
        #     'prefix': 'celery_serverless:watchdog:worker:',
        # }
        ##
        logger.warning('Simulating an Worker invocation: START')

        watchdog_data = kwargs.get('data', {})
        worker_id = watchdog_data.get('worker_id', str(uuid.uuid1()))
        worker_prefix = watchdog_data.get('prefix', None)

        # Worker takes some time to init, then notify the Cache
        time.sleep(2)
        # celeryd_init hook happens at this point

        # Worker connecting to the broker
        time.sleep(2)
        # worker_ready hook happens at this point
        # Delay to acquire the task
        time.sleep(0.5)
        conn.rpop(queue_name)  # Simulate task removal from the queue.

        # task_prerun hook happens at this point
        watchdog.inform_worker_busy(conn, worker_id, prefix=worker_prefix)  # Moves itself from "started" to "working".

        # Worker got a job. Started working
        time.sleep(2)
        # Worker finished the job.

        # task_postrun hook happens at this point
        watchdog.inform_worker_leave(conn, worker_id, prefix=worker_prefix)  # Unsubscribe itself from "working" list.
        logger.warning('Simulating an Worker invocation: END')

    conn.flushdb()
    assert watchdog._get_workers_count(conn) == 0, 'The redis is not starting empty'

    _simulate_worker_invocation()   # Just be sure that it works.

    assert watchdog._get_workers_count(conn) == 0, 'Worker simulation is not informing its finish.'

    jobs = ['one', 'two', 'three']
    with conn.pipeline() as pipe:
        pipe.delete(queue_name)
        pipe.lpush(queue_name, *jobs)
        pipe.llen(queue_name)
        pipe_result = pipe.execute()

    assert pipe_result[-1] == 3, 'Are our Redis misbehaving or something?'

    with ThreadPoolExecutor() as executor:
        monkeypatch.setattr(
            'celery_serverless.watchdog.invoke_worker',
            lambda data: (True, executor.submit(_simulate_worker_invocation, data=data)),
        )

        _env = dict(
            CELERY_SERVERLESS_QUEUE_URL=queue_url,
            CELERY_SERVERLESS_LOCK_URL=queue_url,
            CELERY_SERVERLESS_INTERCOM_URL=queue_url,
        )
        with env.set_env(**_env):
            response = handler_watchdog(None, None)

    assert response
    assert conn.llen(queue_name) == 0, 'Watchdog finished but the queue is not empty'

    # Should I really test for this?
    assert watchdog._get_workers_count(conn) == 0, 'Watchdog finished but not the workers'
