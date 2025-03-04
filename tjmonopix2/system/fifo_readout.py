
# ------------------------------------------------------------
# Copyright (c) All rights reserved
# SiLab, Institute of Physics, University of Bonn
# ------------------------------------------------------------
#

import datetime
import sys
from collections import deque
from queue import Empty, Queue
from threading import Event, Thread
from time import mktime, sleep, time

from tjmonopix2.system import logger

data_iterable = ("data", "timestamp_start", "timestamp_stop", "error")


class FifoError(Exception):
    pass


class EightbTenbError(FifoError):
    pass


class FifoDiscardError(FifoError):
    pass


class NoDataTimeout(Exception):
    pass


class StopTimeout(Exception):
    pass


class FifoReadout(object):
    def __init__(self, daq):
        self.log = logger.setup_derived_logger('FIFO Readout')

        self.daq = daq
        self.callback = None
        self.errback = None
        self.readout_thread = None
        self.worker_thread = None
        self.watchdog_thread = None
        self.fill_buffer = False
        self.readout_interval = 0.05
        self._moving_average_time_period = 10.0
        self._data_deque = deque()
        self._data_buffer = deque()
        self._words_per_read = deque(maxlen=int(self._moving_average_time_period / self.readout_interval))
        self._result = Queue(maxsize=1)
        self._calculate = Event()
        self.stop_readout = Event()
        self.force_stop = Event()
        self.timestamp = None
        self.update_timestamp()
        self._is_running = False
        self.reset_rx()
        self.reset_sram_fifo()
        self._record_count = 0

    @property
    def is_running(self):
        return self._is_running

    @property
    def is_alive(self):
        if self.worker_thread:
            return self.worker_thread.is_alive()
        else:
            False

    @property
    def data(self):
        if self.fill_buffer:
            return self._data_buffer
        else:
            self.log.warning('Data requested but software data buffer not active')

    def data_words_per_second(self):
        if self._result.full():
            self._result.get()
        self._calculate.set()
        try:
            result = self._result.get(timeout=2 * self.readout_interval)
        except Empty:
            self._calculate.clear()
            return None
        return result / float(self._moving_average_time_period)

    def start(self, callback=None, errback=None, reset_rx=False, reset_sram_fifo=False, clear_buffer=False, fill_buffer=False, no_data_timeout=None):
        if self._is_running:
            raise RuntimeError('Readout already running: use stop() before start()')

        self._is_running = True
        self.log.debug('Starting FIFO readout...')
        self.callback = callback
        self.errback = errback
        self.fill_buffer = fill_buffer
        self._record_count = 0
        if reset_rx:
            self.reset_rx()
        if reset_sram_fifo:
            self.reset_sram_fifo()
        else:
            fifo_size = self.daq['FIFO']['FIFO_SIZE']
            if fifo_size != 0:
                self.log.warning('FIFO not empty when starting FIFO readout: size = %i', fifo_size)
        self._words_per_read.clear()
        if clear_buffer:
            self._data_deque.clear()
            self._data_buffer.clear()
        self.stop_readout.clear()
        self.force_stop.clear()
        if self.errback:
            self.watchdog_thread = Thread(target=self.watchdog, name='WatchdogThread')
            self.watchdog_thread.daemon = True
            self.watchdog_thread.start()
        if self.callback:
            self.worker_thread = Thread(target=self.worker, name='WorkerThread')
            self.worker_thread.daemon = True
            self.worker_thread.start()
        self.readout_thread = Thread(target=self.readout, name='ReadoutThread', kwargs={'no_data_timeout': no_data_timeout})
        self.readout_thread.daemon = True
        self.readout_thread.start()

    def stop(self, timeout=10.0):
        if not self._is_running:
            raise RuntimeError('Readout not running: use start() before stop()')
        self._is_running = False
        self.stop_readout.set()
        sleep(0.1)
        try:
            self.readout_thread.join(timeout=timeout)
            if self.readout_thread.is_alive():
                if timeout:
                    raise StopTimeout('FIFO stop timeout after %0.1f second(s)' % timeout)
                else:
                    self.log.warning('FIFO stop timeout')
        except StopTimeout as e:
            self.force_stop.set()
            if self.errback:
                self.errback(sys.exc_info())
            else:
                self.log.error(e)
        if self.readout_thread.is_alive():
            self.readout_thread.join()
        if self.errback:
            self.watchdog_thread.join()
        if self.callback:
            self.worker_thread.join()
        self.callback = None
        self.errback = None
        self.log.debug('Stopped FIFO readout')

    def print_readout_status(self):
        discard_count = self.get_rx_fifo_discard_count()

        if any(discard_count):
            try:
                queue_size = len(self._data_deque)
            except NotImplementedError as e:
                self.log.warning(e)
                queue_size = -1
            self.log.warning('RX errors detected')
            self.log.warning('Recived words:               %d', self._record_count)
            self.log.warning('Data queue size:             %d', queue_size)
            self.log.warning('FIFO size:                   %d', self.daq['FIFO']['FIFO_SIZE'])
            self.log.warning('Channel:                     %s', " | ".join([channel.name.rjust(3) for _, channel in sorted(self.daq.rx_channels.items())]))
            # self.log.warning('RX sync:                     %s', " | ".join(["YES".rjust(3) if status is True else "NO".rjust(3) for status in sync_status]))
            self.log.warning('RX FIFO discard counter:     %s', " | ".join([repr(count).rjust(3) for count in discard_count]))
            # self.log.warning('RX soft errors:              %s', " | ".join([repr(count).rjust(3) for count in soft_error_count]))
            # self.log.warning('RX hard errors:              %s', " | ".join([repr(count).rjust(3) for count in hard_error_count]))

        return discard_count

    def readout(self, no_data_timeout=None):
        '''
            Readout thread continuously reading FIFO. Uses read_data() and appends data to self._data_deque (collection.deque).
        '''
        self.log.debug('Starting %s', self.readout_thread.name)
        curr_time = self.get_float_time()
        time_wait = 0.0
        while not self.force_stop.wait(time_wait if time_wait >= 0.0 else 0.0):
            try:
                time_read = time()
                if no_data_timeout and curr_time + no_data_timeout < self.get_float_time():
                    raise NoDataTimeout('Received no data for %0.1f second(s)' % no_data_timeout)
                data = self.read_data()
                self._record_count += len(data)
            except Exception:
                no_data_timeout = None  # raise exception only once
                if self.errback:
                    self.errback(sys.exc_info())
                else:
                    raise
                if self.stop_readout.is_set():
                    break
            else:
                n_words = data.shape[0]
                last_time, curr_time = self.update_timestamp()
                status = 0
                if self.callback:
                    self._data_deque.append((data, last_time, curr_time, status))
                if self.fill_buffer:
                    self._data_buffer.append((data, last_time, curr_time, status))
                self._words_per_read.append(n_words)
                # FIXME: busy FE prevents scan termination? To be checked
                if self.stop_readout.is_set():
                    break
            finally:
                time_wait = self.readout_interval - (time() - time_read)
            if self._calculate.is_set():
                self._calculate.clear()
                self._result.put(sum(self._words_per_read))
        if self.callback:
            self._data_deque.append(None)  # last item, will stop worker
        self.log.debug('Stopped %s', self.readout_thread.name)

    def worker(self):
        '''
            Worker thread continuously calling callback function when data is available.
        '''
        self.log.debug('Starting %s', self.worker_thread.name)
        while True:
            try:
                data = self._data_deque.popleft()
            except IndexError:
                self.stop_readout.wait(self.readout_interval)  # sleep a little bit, reducing CPU usage
            else:
                if data is None:  # if None then exit
                    break
                else:
                    try:
                        self.callback(data)
                    except Exception:
                        self.errback(sys.exc_info())

        self.log.debug('Stopped %s', self.worker_thread.name)

    def watchdog(self):
        self.log.debug('Starting %s', self.watchdog_thread.name)
        while True:
            try:
                error_count = self.get_rx_8b10b_error_count()
                if any(error_count):
                    raise EightbTenbError('RX 8b10b error(s) detected ', error_count)
                discard_count = self.get_rx_fifo_discard_count()
                if any(discard_count):
                    raise FifoDiscardError('RX FIFO discard error(s) detected ', discard_count)
            except Exception:
                self.errback(sys.exc_info())
            if self.stop_readout.wait(self.readout_interval * 10):
                break
        self.log.debug('Stopped %s', self.watchdog_thread.name)

    def read_data(self):
        '''
            Read FIFO and return data array
            Can be used without threading.

            Returns
            ----------
            data : list
                    A list of FIFO data words.
        '''
        return self.daq['FIFO'].get_data()

    def update_timestamp(self):
        curr_time = self.get_float_time()
        last_time = self.timestamp
        self.timestamp = curr_time
        return last_time, curr_time

    def read_status(self):
        raise NotImplementedError()

    def reset_sram_fifo(self):
        fifo_size = self.daq['FIFO']['FIFO_SIZE']
        self.log.debug('Resetting FIFO: size = %i', fifo_size)
        self.update_timestamp()
        self.daq['FIFO']['RESET']
        sleep(0.01)  # sleep here for a while
        fifo_size = self.daq['FIFO']['FIFO_SIZE']
        if fifo_size != 0:
            self.log.warning('FIFO not empty after reset: size = %i', fifo_size)

    def reset_rx(self, channels=None):
        self.log.debug('Resetting RX')
        if channels:
            [channel for channel in channels if self.daq.rx_channels[channel].reset()]
        else:
            [rx for _, rx in self.daq.rx_channels.items() if rx.reset()]
        sleep(0.1)  # Sleep here for a while

    def get_rx_8b10b_error_count(self, rx_channel=None):
        if rx_channel is None:
            return [rx.get_decoder_error_counter() for _, rx in sorted(self.daq.rx_channels.items())]
        else:
            return self.daq.rx_channels[rx_channel].get_decoder_error_counter()

    def get_rx_fifo_discard_count(self, rx_channel=None):
        if rx_channel is None:
            return [rx.get_lost_data_counter() for _, rx in sorted(self.daq.rx_channels.items())]
        else:
            return self.daq.rx_channels[rx_channel].get_lost_data_counter()

    def get_float_time(self):
        '''
            Returns time as double precision floats - Time64 in pytables - mapping to and from python datetime's
        '''
        t1 = time()
        t2 = datetime.datetime.fromtimestamp(t1)
        return mktime(t2.timetuple()) + 1e-6 * t2.microsecond
