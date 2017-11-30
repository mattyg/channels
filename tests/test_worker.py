from __future__ import unicode_literals

import time
import threading
from contextlib import contextmanager

from asgiref.inmemory import ChannelLayer as InMemoryChannelLayer
from channels import DEFAULT_CHANNEL_LAYER, Channel, route
from channels.asgi import channel_layers, ChannelLayerWrapper
from channels.exceptions import ConsumeLater
from channels.test import ChannelTestCase
from channels.worker import Worker, WorkerGroup, StopWorkerGroupLoop

try:
    from unittest import mock
except ImportError:
    import mock


class PatchedWorker(Worker):
    """Worker with specific numbers of loops"""
    def get_termed(self):
        if not self.__iters:
            return True
        self.__iters -= 1
        return False

    def set_termed(self, value):
        self.__iters = value

    termed = property(get_termed, set_termed)


class WorkerTests(ChannelTestCase):
    """
    Tests that the router's routing code works correctly.
    """

    def test_channel_filters(self):
        """
        Tests that the include/exclude logic works
        """
        # Include
        worker = Worker(None, only_channels=["yes.*", "maybe.*"])
        self.assertEqual(
            worker.apply_channel_filters(["yes.1", "no.1"]),
            ["yes.1"],
        )
        self.assertEqual(
            worker.apply_channel_filters(["yes.1", "no.1", "maybe.2", "yes"]),
            ["yes.1", "maybe.2"],
        )
        # Exclude
        worker = Worker(None, exclude_channels=["no.*", "maybe.*"])
        self.assertEqual(
            worker.apply_channel_filters(["yes.1", "no.1", "maybe.2", "yes"]),
            ["yes.1", "yes"],
        )
        # Both
        worker = Worker(None, exclude_channels=["no.*"], only_channels=["yes.*"])
        self.assertEqual(
            worker.apply_channel_filters(["yes.1", "no.1", "maybe.2", "yes"]),
            ["yes.1"],
        )

    def test_run_with_consume_later_error(self):

        # consumer with ConsumeLater error at first call
        def _consumer(message, **kwargs):
            _consumer._call_count = getattr(_consumer, '_call_count', 0) + 1
            if _consumer._call_count == 1:
                raise ConsumeLater()

        Channel('test').send({'test': 'test'}, immediately=True)
        channel_layer = channel_layers[DEFAULT_CHANNEL_LAYER]
        channel_layer.router.add_route(route('test', _consumer))
        old_send = channel_layer.send
        channel_layer.send = mock.Mock(side_effect=old_send)  # proxy 'send' for counting

        worker = PatchedWorker(channel_layer)
        worker.termed = 2  # first loop with error, second with sending

        worker.run()
        self.assertEqual(getattr(_consumer, '_call_count', None), 2)
        self.assertEqual(channel_layer.send.call_count, 1)

    def test_normal_run(self):
        consumer = mock.Mock()
        Channel('test').send({'test': 'test'}, immediately=True)
        channel_layer = channel_layers[DEFAULT_CHANNEL_LAYER]
        channel_layer.router.add_route(route('test', consumer))
        old_send = channel_layer.send
        channel_layer.send = mock.Mock(side_effect=old_send)  # proxy 'send' for counting

        worker = PatchedWorker(channel_layer)
        worker.termed = 2

        worker.run()
        self.assertEqual(consumer.call_count, 1)
        self.assertEqual(channel_layer.send.call_count, 0)


@contextmanager
def test_channel_layer(name):
    """Setup test channel to apply custom routing."""
    test_layer = ChannelLayerWrapper(
        channel_layer=InMemoryChannelLayer(),
        alias=name, routing=[],
    )
    old_layer = channel_layers.set(name, test_layer)
    yield test_layer
    channel_layers.set(name, old_layer)


@contextmanager
def test_worker_group(layer, n_threads, callback=None):
    """Setup test worker group and validate it's finished."""
    worker_group = WorkerGroup(layer,
                               n_threads=n_threads,
                               signal_handlers=False,
                               stop_gracefully=True,
                               callback=callback)
    worker_group_t = threading.Thread(target=worker_group.run)
    worker_group_t.daemon = True
    worker_group_t.start()
    yield worker_group
    worker_group_t.join()
    for worker_id in range(len(worker_group.workers)):
        assert worker_group.workers[worker_id].in_job is False
        assert worker_group.threads[worker_id].is_alive() is False


class WorkerGroupTests(ChannelTestCase):

    CALLBACK_TIME_LIMIT = 1  # seconds

    def _tracked_callback(self):
        """
        Helper to create a callback with tracking logic based on events:
        it allows to wait for callback start and check that it's completed.
        """
        is_running = threading.Event()
        is_stopped = threading.Event()

        def callback(channel, message):
            is_running.set()
            # emulate some delay to validate graceful stop
            time.sleep(0.1)
            is_stopped.set()

        return callback, (is_running, is_stopped)

    def test_graceful_stop_when_main_worker_is_idle(self):
        """
        Test to stop a worker group when main worker is idle, there must be
        an exception to break main loop.
        """
        with test_channel_layer('test') as channel_layer:
            with test_worker_group(channel_layer, n_threads=1) as worker_group:
                self.assertRaises(StopWorkerGroupLoop,
                                  worker_group.sigterm_handler, None, None)

    def test_graceful_stop_when_waiting_for_main_worker(self):
        """
        Test to stop a worker group when main worker is processing a message.
        SIGTERM handler shouldn't raise an exception allowing to finish
        processing the message and exit gracefully.
        """
        callback, (cb_is_running, cb_is_stopped) = self._tracked_callback()

        with test_channel_layer('test') as channel_layer:
            Channel('test', alias='test').send({'test': 'test'}, immediately=True)
            consumer = mock.Mock()
            channel_layer.router.add_route(route('test', consumer))
            # proxy 'send' for counting
            channel_layer.send = mock.Mock(side_effect=channel_layer.send)

            with test_worker_group(channel_layer, n_threads=1,
                                   callback=callback) as worker_group:
                self.assertTrue(cb_is_running.wait(self.CALLBACK_TIME_LIMIT))
                # main worker processing msg, wait for it and exit gracefully
                worker_group.sigterm_handler(None, None)
                self.assertTrue(cb_is_stopped.wait(self.CALLBACK_TIME_LIMIT))

            self.assertEqual(consumer.call_count, 1)
            self.assertEqual(channel_layer.send.call_count, 0)

    def test_graceful_stop_with_multiple_threads(self):
        """
        Test that the whole worker group is stopped gracefully on termination
        signal: it should finish processing current messages and exit.
        """
        callback, (cb_is_running, cb_is_stopped) = self._tracked_callback()

        with test_channel_layer('test') as channel_layer:
            Channel('test', alias='test').send({'test': 'test'}, immediately=True)
            consumer = mock.Mock()
            channel_layer.router.add_route(route('test', consumer))
            # proxy 'send' for counting
            channel_layer.send = mock.Mock(side_effect=channel_layer.send)

            with test_worker_group(channel_layer, n_threads=3,
                                   callback=callback) as worker_group:
                self.assertTrue(cb_is_running.wait(self.CALLBACK_TIME_LIMIT))
                # sub-workers threads are started before main thread and most
                # often pick the message, so main thread is idle on termination
                # signal and causes raising StopWorkerGroupLoop, but that's not
                # always the case and sometimes main thread picks the message
                # and exception is not raised.
                try:
                    worker_group.sigterm_handler(None, None)
                except StopWorkerGroupLoop:
                    pass
                self.assertTrue(cb_is_stopped.wait(self.CALLBACK_TIME_LIMIT))

            self.assertEqual(consumer.call_count, 1)
            self.assertEqual(channel_layer.send.call_count, 0)
