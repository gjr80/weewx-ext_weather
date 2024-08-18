"""
This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU General Public License for more details.

                      Installer for External Weather service

Version: 0.3.0a1                                       Date: xx August 2024

Revision History
    xx August 2024      v0.3.0
        -   initial implementation (no installer for earlier versions)
"""

# python imports
import configobj
from setup import ExtensionInstaller

# import StringIO, use six.moves due to python2/python3 differences
from six.moves import StringIO

# WeeWX imports
import weewx


REQUIRED_WEEWX_VERSION = "3.0.0"
WEATHER_API_VERSION = "0.3.0a1"
# define our config as a multiline string so we can preserve comments
ext_weather_config = """
[WeatherAPI]
    # This section is for the External Weather API service.

    # debug setting fpr all sources, may be 0, 1, 2 or 3, default is 0
    debug = 0
    
    [[XWeatherMap]]
        source = XWM
        
        # enable or disable this source, may be True or False
        enable = False
        
        # debug setting for this source, overrides any setting further up the 
        # tree, may be 0, 1, 2 or 3, default is 0
        debug = 0       
        
        # XWeather client ID
        id = client_id
        
        # XWeather client secret
        secret = client_secret
        
        # portion of the API request URL that defines the map options being 
        # used
        url_stem = URL map options
        
        # destination directory for downloaded map file
        destination = /var/tmp
        
        # interval in seconds between API requests, default is 1800
        interval = 1800
        
        # minimum period (seconds) between successive API requests, default 
        # is 10
        api_lockout_period = 10
        
        # maximum number of attempts to obtain an API response before giving up
        max_tries = 3
        
        # WeeWX event to bind to for queue processing/checking, may 
        # be NEW_LOOP_PACKET, CHECK_LOOP, NEW_ARCHIVE_RECORD or POST_LOOP
        check_queue_event = CHECK_LOOP  
"""

# construct our config dict
ext_weather_dict = configobj.ConfigObj(StringIO(ext_weather_config))


def version_compare(v1, v2):
    """Basic 'distutils' and 'packaging' free version comparison.

    v1 and v2 are WeeWX version numbers in string format.

    Returns:
        0 if v1 and v2 are the same
        -1 if v1 is less than v2
        +1 if v1 is greater than v2
    """

    import itertools
    mash = itertools.zip_longest(v1.split('.'), v2.split('.'), fillvalue='0')
    for x1, x2 in mash:
        if x1 > x2:
            return 1
        if x1 < x2:
            return -1
    return 0


def loader():
    return ExtWeatherInstaller()


class ExtWeatherInstaller(ExtensionInstaller):
    def __init__(self):
        if version_compare(weewx.__version__, REQUIRED_WEEWX_VERSION) < 0:
            msg = "%s %s requires WeeWX %s or greater, found %s" % ('External Weather API service',
                                                                    WEATHER_API_VERSION,
                                                                    REQUIRED_WEEWX_VERSION,
                                                                    weewx.__version__)
            raise weewx.UnsupportedFeature(msg)
        super(ExtWeatherInstaller, self).__init__(
            version=WEATHER_API_VERSION,
            name='External Weather API',
            description='WeeWX service for obtaining data from an external Weather API.',
            author="Gary Roderick",
            author_email="gjroderick<@>gmail.com",
            files=[('bin/user', ['bin/user/weatherapi.py'])],
            config=ext_weather_dict
        )
