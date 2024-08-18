"""
weatherapi.py

A WeeWX service to augment loop packets and archive records with data from an
external weather API.

This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details.

Version: 0.3.0a1                                    Date: xx August 2024

Revision History
    xx August 2024      v0.3.0
        - restructured such that a single service (WeatherApiService) now runs
          multiple threads with one weather API supported per thread
        - change Collector/collector terminology to Source/source
    13 August 2024      v0.2.0
        - refactored to use python 3.6 or later
        - refactored thread management to better handle thread closure
        - implemented a single XWeather Map service
        - removed six.moves dependency
    2 April 2023        v0.1.0
        - initial implementation
"""
#TODO. Need to sort out debug level in the service vs debug in the threads

# python imports
import os
import os.path
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

# WeeWX imports
import weewx
import weeutil.config
import weeutil.weeutil
import weewx.engine

# import/setup logging, WeeWX v3 is syslog based but WeeWX v4 is logging based,
# try v4 logging and if it fails use v3 logging
try:
    # WeeWX4 logging
    import logging
    from weeutil.logger import log_traceback
    log = logging.getLogger(__name__)

    def logcrit(msg):
        log.critical(msg)

    def logdbg(msg):
        log.debug(msg)

    def logerr(msg):
        log.error(msg)

    def loginf(msg):
        log.info(msg)

    # log_traceback() generates the same output but the signature and code is
    # different between v3 and v4. Define suitable wrapper functions for those
    # levels needed.
    def log_traceback_critical(prefix=''):
        log_traceback(log.critical, prefix=prefix)

    def log_traceback_error(prefix=''):
        log_traceback(log.error, prefix=prefix)

except ImportError:
    # WeeWX legacy (v3) logging via syslog
    import syslog
    from weeutil.weeutil import log_traceback

    def logmsg(level, msg):
        syslog.syslog(level, 'weatherapi: %s' % msg)

    def logcrit(msg):
        logmsg(syslog.LOG_CRIT, msg)

    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)

    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)

    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)

    # log_traceback() generates the same output but the signature and code is
    # different between v3 and v4. Define suitable wrapper functions for those
    # levels needed.
    def log_traceback_critical(prefix=''):
        log_traceback(prefix=prefix, loglevel=syslog.LOG_CRIT)

    def log_traceback_error(prefix=''):
        log_traceback(prefix=prefix, loglevel=syslog.LOG_ERR)

WEATHER_API_VERSION = "0.3.0a1"

# ============================================================================
#                                class Source
# ============================================================================

class Source(object):
    """Base class for a threaded client that polls an API."""

    def __init__(self, source_dict, control_queue, data_queue):
        self.control_queue = control_queue
        self.data_queue = data_queue
        self.thread = None
        self.debug = weeutil.weeutil.to_int(source_dict.get('debug', 0))
        self.name = source_dict.get('name', 'api_source')
        self.max_tries = weeutil.weeutil.to_int(source_dict.get('max_tries', 2))
        self.collect_data = False

    def collect(self):
        """Entry point for the thread."""

        # since we are in a thread some additional try..except clauses will
        # help give additional output in case of an error rather than having
        # the thread die silently
        try:
            # first run our setup() method
            self.setup()
            # collect data continuously while we are told to collect data
            while self.collect_data:
                # run an inner loop obtaining, parsing and dispatching the data
                # and checking for the shutdown signal
                # first up get the raw data
                _raw_data = self.get_raw_data()
                # if we have a non-None response then we have data so parse it,
                # gather the required data and put it in the result queue
                if _raw_data is not None:
                    # parse the raw data response and extract the required data
                    _data = self.parse_raw_data(_raw_data)
                    if self.debug > 0:
                        loginf("%s: Parsed data=%s" % (self.name, _data))
                    # now process the parsed data
                    self.process_data(_data)
                # sleep for a second and then see if it's time to poll again
                time.sleep(1)
        except Exception as e:
            # Some unknown exception occurred. This is probably a serious
            # problem. Exit with some notification.
            logcrit("%s: Unexpected exception of type %s" % (self.name, type(e)))
            log_traceback_critical(prefix='%s: **** ' % self.name)
            logcrit("%s: Thread exiting. Reason: %s" % (self.name, e))

    def setup(self):
        """Perform any post-initialisation setup.

        This method is executed as the very first thing in the thread run()
        method. It must be defined if required for each child class.
        """

        pass

    def get_raw_data(self):
        """Obtain the raw API data.

        This method must be defined for each child class.
        """

        return None

    def parse_raw_data(self, response):
        """Parse the raw API data and return the required data.

        This method must be defined if the raw API data must be further
        processed to extract the desired data. If this method is not overridden
        the raw API data is returned unchanged.
        """

        return response

    def process_data(self, data):
        """Process the parsed data.

        The default action of this method is to package the data into a dict
        and place it in the queue for our parent Service to further process.

        This method should be overridden if other tasks are to be performed
        with the data.
        """

        # if we have some data then place it in the result queue
        if data is not None:
            # construct our data dict for the queue
            _package = {'type': 'data',
                        'payload': data}
            self.queue.put(_package)

    def submit_request(self, url, headers=None):
        """Submit a HTTP GET API request with retries and return the result.

        Submit a HTTP GET request using the supplied URL and optional header
        dict. If the API does not respond the request will be submitted up to a
        total of self.max_tries times before giving up. If a response is
        received it is character decoded and returned. If no response is
        received None is returned.
        """

        if headers is None:
            headers = {}
        # obtain a Request object
        req = urllib.request.Request(url=url, headers=headers)
        # we will attempt to obtain a response max_tries times
        for count in range(self.max_tries):
            # attempt to contact the API
            try:
                w = urllib.request.urlopen(req)
            except urllib.error.HTTPError as err:
                logerr("%s: Failed to get API response on attempt %d" % (self.name,
                                                                         count + 1,))
                logerr("   **** %s" % err)
            except (urllib.error.URLError, socket.timeout) as e:
                logerr("%s: Failed to get API response on attempt %d" % (self.name,
                                                                         count + 1,))
                logerr("%s:   **** %s" % (self.name, e))
            else:
                # We have a response, but it could be character set encoded.
                # Get the charset used so we can decode the stream correctly.
                char_set = w.headers.get_content_charset()
                # now get the response, decoding it appropriately
                response = w.read().decode(char_set)
                # close the API connection
                w.close()
                # log the decoded response if required
                if self.debug >= 2:
                    log.info("%s: API response=%s" % (self.name, response))
                # return the decoded response
                return response
        else:
            # no response after max_tries attempts, so log it
            logerr("%s: Failed to get API response" % self.name)
        # if we made it here we have not been able to obtain a response so
        # return None
        return None

    @staticmethod
    def obfuscated(secret):
        """Produce an obfuscated copy of a secret.

        Obfuscates a number of the leftmost characters in a string leaving a
        number of the rightmost characters as is. For strings of length eight
        characters or fewer at least half of the characters are obfuscated. For
        strings longer than eight characters in length all except the rightmost
        four characters are obfuscated. If the string is None or the length of
        the string is less than 2 then None is returned.
        """

        if secret is None or len(secret) < 2:
            return None
        elif len(secret) < 8:
            clear = len(secret) // 2
        else:
            clear = 4
        return '*' * (len(secret) - clear) + secret[-clear:]

    def startup(self, name):
        """Start the thread that collects data from the API.

        Start the source in a daemonised thread and start collecting data.
        Child classes may should override this method if required.
        """

        # wrap in a try .. except in case there is a problem starting the
        # thread
        try:
            # obtain a SourceThread object
            self.thread = Source.SourceThread(self)
            # tell the thread to start collecting data
            self.collect_data = True
            # daemonise the thread
            self.thread.daemon = True
            # assign a name to the thread
            self.thread.name = name
            # start the thread running
            self.thread.start()
        except threading.ThreadError:
            # we have a threading error that prevented the thread from being
            # created, log it
            logerr("Unable to launch %s thread" % name)
            # and set our thread to None
            self.thread = None

    def shutdown(self):
        """Shut down the thread that collects data from the API.

        Tell the thread to stop, then wait for it to finish.
        """

        # we only need do something if a thread exists
        if self.thread:
            name = self.thread.name
            # tell the thread to stop collecting data
            self.collect_data = False
            # terminate the thread
            self.thread.join(10.0)
            # log the outcome
            if self.thread.is_alive():
                logerr("Unable to shut down %s" % name)
            else:
                loginf("%s has been terminated" % name)
        self.thread = None

    class SourceThread(threading.Thread):
        """Class using a thread to collect data via an API."""

        def __init__(self, client):
            # initialise our parent
            threading.Thread.__init__(self)
            # keep reference to the client we are supporting
            self.client = client

        def run(self):
            # rather than letting the thread silently fail if an exception
            # occurs within the thread, wrap in a try..except so the exception
            # can be caught and available exception information displayed
            try:
                # kick the collection off
                self.client.collect()
            except Exception:
                # we have an exception so log what we can
                log_traceback_critical('    ****  ')


# ============================================================================
#                          class WeatherApiService
# ============================================================================

class WeatherApiService(weewx.engine.StdService):
    """Class to obtain weather data from one or more weather APIs.

    [WeatherAPI]
        [[SomeName]]
            source = abc
            enable = True | False
            debug = 0 | 1 | 2 | 3
            ....
            check_queue_event = NEW_LOOP_PACKET | CHECK_LOOP |
                                NEW_ARCHIVE_RECORD | POST_LOOP
            [field_map]]
                weewx_field = api_field
                unit = weewx_unit_name

    """

    def __init__(self, engine, config_dict):
        # initialise our superclass
        super(WeatherApiService, self).__init__(engine, config_dict)
        # obtain the WeatherApi config stanza
        api_config = config_dict.get('WeatherAPI')
        # we can only continue is we have a config stanza, if we don't have a
        # config stanza we're done
        if api_config is None:
            return
        # obtain our debug value
        self.debug = weeutil.weeutil.to_int(api_config.get('debug', 0))
        if weewx.debug >= 1 or self.debug >= 1:
            log.info("WeatherApiService v%s" % WEATHER_API_VERSION)
        # initialise a dict to hold our active sources
        self.sources = dict()
        # iterate over the sections of our config, each section is the config
        # for one source
        for source in api_config.sections:
            # get the source config with accumulated leaves
            source_config = weeutil.config.accumulateLeaves(api_config[source])
            # log the config if debug >= 2
            if self.debug >= 2:
                log.info("WeatherApiService: '%s' config=%s" % (source,
                                                                source_config))
            # is this source enabled
            enable = weeutil.weeutil.to_bool(source_config.get('enable',
                                                               False))
            if 'source' in source_config and source_config['source'] in KNOWN_SOURCES.keys() and enable:
                # we have a source config dict and the source is enabled
                # obtain Queue objects for the control and data queues
                control_queue = queue.Queue()
                data_queue = queue.Queue()
                # now get an appropriate threaded source object
                source_obj = self.source_factory(config_dict,
                                                 source_config,
                                                 control_queue,
                                                 data_queue)
                # did we get a source object, if we didn't then log it and
                # continue to the next source config
                if source_obj is None:
                    loginf("Source config [[%s]] ignored. Unsupported "
                           "source '%s'." % (source,
                                             source_config['source']))
                    continue
                # otherwise we have a source object, so add it to our dict of
                # active sources
                self.sources[source] = {'name': source_config.get('name',
                                                                  source),
                                        'obj': source_obj,
                                        'control': control_queue,
                                        'data': data_queue,
                                        'event': source_config.get('check_queue_event',
                                                                   'NEW_LOOP_PACKET').upper()
                                        }
                # now start the sources' thread
                self.sources[source]['obj'].startup(self.sources[source]['name'])
                # finally, let the user know what we did
                loginf("Started source '%s'" % self.sources[source]['name'])
            elif 'source' not in source_config:
                # no source was specified
                loginf("No source specified in section [[%s]]" % source)
            elif source_config['source'] not in KNOWN_SOURCES.keys():
                # an invalid source was specified
                loginf("Invalid source '%s' specified" % source_config['source'])
            elif not enable:
                # the source was not enabled
                loginf("Source '%s' ignored, not enabled" % source_config['source'])
            else:
                # we should not end up here
                loginf("Source '%s' ignored" % source_config['source'])
        # initialise a list to hold a list of sources for deletion
        self.pop_list = list()
        # set event bindings
        # bind to NEW_LOOP_PACKET
        self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)
        # bind to CHECK_LOOP
        self.bind(weewx.CHECK_LOOP, self.check_loop)
        # bind to NEW_ARCHIVE_RECORD
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        # bind to POST_LOOP
        self.bind(weewx.POST_LOOP, self.post_loop)

    @staticmethod
    def source_factory(config_dict, source_config, control_queue, data_queue):
        """Factory method to produce a weather API source object."""

        # get the source class name
        source_class = KNOWN_SOURCES.get(source_config['source'])['class']
        if source_class is not None:
            # get the source object
            source_object = source_class(config_dict,
                                         source_config,
                                         control_queue,
                                         data_queue)
            # return the source object
            return source_object
        else:
            # source class is None - we have no class for this source
            return None

    def new_loop_packet(self, event):
        """Process the sources bound to a new loop packet being created.

        Iterate over the list of active sources and process the queue for those
        sources bound to the NEW_LOOP_PACKET event.
        """

        # iterate over the active sources
        for source_key, source in self.sources.items():
            # is the source 'bound' to the NEW_LOOP_PACKET event
            if source['event'] == 'NEW_LOOP_PACKET':
                # the source is 'bound' to the NEW_LOOP_PACKET event so process
                # the queues for that source
                self.process_queue(source_key)
        # remove any closed sources
        self.pop_closed_sources()

    def check_loop(self, event):
        """Process the sources bound to the completion of loop packet processing.

        Iterate over the list of active sources and process the queue for those
        sources bound to the CHECK_LOOP event.
        """

        # iterate over the active sources
        for source_key, source in self.sources.items():
            # is the source 'bound' to the CHECK_LOOP event
            if source['event'] == 'CHECK_LOOP':
                # the source is 'bound' to the CHECK_LOOP event so process
                # the queues for that source
                self.process_queue(source_key)
        # remove any closed sources
        self.pop_closed_sources()

    def new_archive_record(self, event):
        """Process the sources bound to a new archive record being created.

        Iterate over the list of active sources and process the queue for those
        sources bound to the NEW_ARCHIVE_RECORD event.
        """

        # iterate over the active sources
        for source_key, source in self.sources.items():
            # is the source 'bound' to the NEW_ARCHIVE_RECORD event
            if source['event'] == 'NEW_ARCHIVE_RECORD':
                # the source is 'bound' to the NEW_ARCHIVE_RECORD event so process
                # the queues for that source
                self.process_queue(source_key)
        # remove any closed sources
        self.pop_closed_sources()

    def post_loop(self, event):
        """Process the sources bound to the breaking of main processing loop.

        Iterate over the list of active sources and process the queue for those
        sources bound to the POST_LOOP event.
        """

        # iterate over the active sources
        for source_key, source in self.sources.items():
            # is the source 'bound' to the POST_LOOP event
            if source['event'] == 'POST_LOOP':
                # the source is 'bound' to the POST_LOOP event so process
                # the queues for that source
                self.process_queue(source_key)
        # remove any closed sources
        self.pop_closed_sources()

    def process_queue(self, source_key):
        """Process the data queue for a given source."""

        # obtain the source data from the list of active sources
        source = self.sources[source_key]
        # process the queue until it is empty
        while True:
            # Get the next item from the queue. Wrap in a try .. except to
            # catch any instances where the queue is empty as that is our
            # signal to break out of the while loop.
            try:
                # get the next item from the queue, but don't dwell very long
                _data = source['data'].get(True, 0.5)
            except queue.Empty:
                # the queue is empty, that may be because there was never
                # anything in the queue or because we have already processed
                # any queued data; either way log if necessary and break out of
                # the while loop
                if self.debug >= 1:
                    loginf('WeatherApiService: %s: No queued items to process' % source['name'])
                # now break out of the while loop
                break
            else:
                # We received something in the queue, it will be one of three
                # things:
                # 1. data from the API
                # 2. an exception
                # 3. the value None signalling a serious error that means the
                #    source thread needs to shut down

                # if it is 'data' it will be in a dict and have keys
                if hasattr(_data, 'keys'):
                    # obtain the process function for this source
                    process_fn = getattr(self, '_'.join(['process', source['source'].lower()]))
                    # call the process function
                    process_fn(_data)

                # if it's a tuple then it's a tuple with an exception and
                # exception text
                elif isinstance(_data, BaseException):
                    # We have an exception. The source did not deem it serious
                    # enough to want to shut down, or it would have sent None
                    # instead. The action we take depends on the type of
                    # exception it is and the source.
                    self.process_queued_exception(_data, source_key)

                # if it's None then it's a signal the source needs to shut down
                elif _data is None:
                    # log what we received
                    loginf('WeatherApiService: %s: Received source '
                           'shutdown signal' % source['name'])
                    # we received the signal that the source needs to shut
                    # down, that means we cannot continue so call our shutdown
                    # method which will also shut down the relevant source
                    # thread
                    self.shut_down_source(source)
                    # we have told the source to close, mark the source for
                    # deletion when we have finished processing all of our
                    # sources
                    self.pop_list.append(source_key)
                    # The source has been shut down, so we will not see
                    # anything more in the queue. We are still bound to an
                    # event, but since the queue is always empty we will just
                    # wait for the empty queue timeout before exiting

                # if it's none of the above (which it should never be) we don't
                # know what to do with it so pass and wait for the next item in
                # the queue
                else:
                    pass

    def process_queued_exception(self, e, source_key):
        """Process an exception received in a source queue."""

        # we have no exceptions particular to our service, so just log the
        # error and continue
        logerr('WeatherApiService: %s: Source thread caught unexpected'
               ' exception %s: %s' % (self.sources[source_key]['name'],
                                      e.__class__.__name__,
                                      e))

    def pop_closed_sources(self):
        """Remove any closed sources from the list of active sources."""

        # iterate over the list of closed sources
        for source_key in self.pop_list:
            # remove the closed source from the active sources list
            _ignore = self.sources.pop(source_key, None)
        # we are done, reset the pop list
        self.pop_list = list()

    def shut_down_source(self, source_dict):
        """Shut down a source."""

        # The source will likely be running in a thread. Call its shutdown()
        # method so that any thread shut down/tidy up can occur
        source_dict['obj'].shutdown()

    def shutDown(self):
        """Shut down the service."""

        # Any active sources will likely still be running in their own thread.
        # Iterate over any sources still active and shut down each so that any
        # thread shut down/tidy up can occur
        for source_key in self.sources:
            self.shut_down_source(self.sources[source_key])

    def noop(self, *args, **kwargs):
        """Dummy method to accept any parameters but do nothing."""

        pass

    # XWeather maps does no processing of received data (other than saving a
    # file) so set the process function to do nothing
    process_xwm = noop


# ============================================================================
#                          class XWeatherMapSource
# ============================================================================

class XWeatherMapSource(Source):
    """Threaded class to obtain weather map images via the XWeather API."""

    # XWeather map API end point
    END_POINT = 'https://maps.aerisapi.com'

    def __init__(self, config_dict, source_dict, control_queue, data_queue):
        # initialize my base class
        super(XWeatherMapSource, self).__init__(source_dict, control_queue, data_queue)
        # get the ID, secret and URL stem to be used, wrap is a try..except
        # to simplify processing if one or both config items are missing
        try:
            self.id = source_dict['id']
            self.secret = source_dict['secret']
            url_stem = source_dict['url_stem']
        except KeyError:
            log.error('XWeatherMapSource: ID and/or secret and/or '
                      'URL stem not specified. Exiting.')
            # we cannot continue, just return and this service will remain
            # in the list of services but in effect it will do nothing
            return
        # get an identifying prefix to use to identify this thread and when
        # logging
        self.name = 'XWeatherMapSource'
        # construct the ID and secret string to be used in our URL
        id_secret = '_'.join([self.id, self.secret])
        # now construct the URL we will use for our map
        self.url = '/'.join([self.END_POINT, id_secret, url_stem])
        # obtain the HTML_ROOT setting from StdReport
        html_root = os.path.join(config_dict['WEEWX_ROOT'],
                                 config_dict['StdReport']['HTML_ROOT'])
        # obtain the destination for the retrieved file
        _path = source_dict.get('destination', html_root).strip()
        _file = os.path.basename(url_stem)
        # now we can construct the destination path and file name
        self.destination = os.path.join(html_root, _path, _file)
        _path, _file = os.path.split(self.destination)
        if not os.path.exists(_path):
            if self.debug >= 1:
                logdbg("XWeatherMapSource: Creating destination path '%s'" % _path)
            os.makedirs(_path)
        # interval between API calls, default to 30 minutes
        self.interval = weeutil.weeutil.to_int(source_dict.get('interval',
                                                               1800))
        # Get API call lockout period. This is the minimum period between API
        # calls. This prevents an error condition making multiple rapid API
        # calls and thus potentially breaching the API usage conditions.
        # The Aeris Weather API does specify a plan dependent figure for
        # the maximum API calls per minute, we will be conservative and
        # default limit our calls to no more often than once every 10
        # seconds. The user can increase or decrease this value.
        self.lockout_period = weeutil.weeutil.to_int(source_dict.get('api_lockout_period',
                                                                     10))
        # maximum number of attempts to obtain a response from the API
        # before giving up
        self.max_tries = weeutil.weeutil.to_int(source_dict.get('max_tries',
                                                                3))
        # initialise a property to hold the timestamp the API was last called
        self.last_call_ts = None

        # inform the user what we are doing, what we log depends on the
        # WeeWX debug level or our debug level
        # basic infor is logged everytime
        loginf("'%s' will obtain XWeather map image data" % self.name)
        loginf("    destination=%s interval=%d" % (self.destination,
                                                   self.interval))
        # more detail when debug >= 1
        if weewx.debug >= 1 or self.debug >= 1:
            loginf("    XWeather debug=%d lockout period=%s max tries=%s" % (self.debug,
                                                                             self.lockout_period,
                                                                             self.max_tries))
        # detailed client and URL info when debug >= 2
        if weewx.debug >= 2 or self.debug >= 2:
            loginf("    client ID=%s" % self.obfuscated(self.id))
            loginf("    client secret=%s" % self.obfuscated(self.secret))
            loginf("    URL stem=%s" % url_stem)
        # create a thread property
        self.thread = None
        # we start off not collecting data, it will be turned on later when we
        # are threaded
        self.collect_data = False

    def get_raw_data(self):
        """Make a data request via the API.

        Submit an API request observing both lock periods and the API call
        interval.

        For an XWeather map API call the resulting map image file is saved as
        part of the API call HTTP request and as such no API response data is
        returned that requires further processing by our parent. To this end
        all processing and logging is carried out in this or subordinate
        methods, and we return the value None in all cases.
        """

        # get the current time
        now = time.time()
        # log the time of last call if debug >= 2
        if weewx.debug >= 2 or self.debug >= 2:
            log.debug("Last %s API call at %s" % (self.name, self.last_call_ts))
        # has the lockout period passed since the last call
        if self.last_call_ts is None or ((now + 1 - self.lockout_period) > self.last_call_ts):
            # If we haven't made an API call previously, or if it's been too
            # long since the last call then make the call
            if (self.last_call_ts is None) or ((now + 1 - self.interval) >= self.last_call_ts):
                # if debug >= 2 log the URL used, but obfuscate the client
                # credentials
                if weewx.debug >= 2 or self.debug >= 2:
                    _obfuscated = self.url.replace(self.id,
                                                   self.obfuscated(self.id))
                    _obfuscated_url = _obfuscated.replace(self.secret,
                                                          self.obfuscated(self.secret))
                    log.info("Submitting %s API call using URL: %s" % (self.name,
                                                                       _obfuscated_url))
                # make the API call and return the response, we will discard the
                # response after some response based logging
                _result = self.submit_request()
                # log the result
                if weewx.debug >= 1 or self.debug >= 1:
                    if _result is not None:
                        loginf("%s: successfully downloaded '%s'" % (self.name, _result))
                    else:
                        loginf("%s: failed to obtain API response" % self.name)
        # we have nothing to return, so return None
        return None

    def submit_request(self):
        """Submit a HTTP GET request to the API.

        Normally submit_request would accept a URL and headers dict and handle
        submitting a request to the API with retries. However, for this API our
        URL is fixed and as we use the urllib.request.urlretrieve() method to
        retrieve a file there are no headers involved. Consequently, we have a
        different signature to the same method in our parent.

        Download and save the file generated by the API request. If this file
        is downloaded and saved successfully the file name and path is
        returned. If a HTTP, URL or socket error is encountered or another
        exception raised, the value None is returned.
        """

        # try to make the request up to self.max_tries times
        for count in range(self.max_tries):
            # attempt to contact the API
            try:
                # make the call, urlretrieve returns the file name and headers
                (file_name, headers) = urllib.request.urlretrieve(self.url,
                                                                  self.destination)
            except urllib.error.HTTPError as err:
                log.error("Failed to get %s API response on attempt %d" % (self.name,
                                                                           count + 1))
                log.error("   **** %s" % err)
                if err.code == 403:
                    # XWeather has not listed specific HTTP response error
                    # codes, but we know an incorrect client ID or secret results
                    # in a 403 error as does an invalid URL stem
                    log.error("   **** Possible incorrect client credentials or URL stem")
                    # we cannot continue with these errors so return
                    break
            except (urllib.error.URLError, socket.timeout) as e:
                log.error("Failed to get %s API response on attempt %d" % (self.name,
                                                                           count + 1))
                log.error("   **** %s" % e)
            except Exception as e:
                log.error("An unexpected error occurred on %s API "
                          "response attempt %d" % (self.name, count + 1))
                log.error("   **** %s" % e)
            else:
                # we had a successful retrieval, first update the time of last call
                self.last_call_ts = time.time()
                # indicate success to our caller by returning the file name
                return file_name
        else:
            # no response after max_tries attempts, so log it
            log.error("Failed to get %s API response after %d attempts" % (self.name,
                                                                           self.max_tries))
        # if we made it here we have nothing so return None
        return None


# ============================================================================
#                          class XWeatherMapSource
# ============================================================================

class OpenWeatherSource(Source):
    """Threaded class that obtains weather data via the OpenWeather API."""

    def __init__(self, config_dict, source_dict, control_queue, data_queue):
            # initialize my base class
            super(OpenWeatherSource, self).__init__(source_dict, control_queue, data_queue)


KNOWN_SOURCES = {'OW': {'class': OpenWeatherSource},
                 'XWM': {'class': XWeatherMapSource}
                 }