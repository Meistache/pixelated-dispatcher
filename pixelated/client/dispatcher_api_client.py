#
# Copyright (c) 2014 ThoughtWorks Deutschland GmbH
#
# Pixelated is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pixelated is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Pixelated. If not, see <http://www.gnu.org/licenses/>.
import json
import ssl
import time

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError
from pixelated.common import latest_available_ssl_version
from pixelated.common import logger
try:
    from requests.packages.urllib3.poolmanager import PoolManager
except:
    from urllib3.poolmanager import PoolManager

DEFAULT_TIMEOUT_IN_S = 10
VERIFY_HOSTNAME = None


class EnforceTLSv1Adapter(HTTPAdapter):
    __slots__ = ('_assert_hostname', '_assert_fingerprint')

    def __init__(self, assert_hostname=VERIFY_HOSTNAME, assert_fingerprint=None):
        self._assert_hostname = assert_hostname
        self._assert_fingerprint = assert_fingerprint
        super(EnforceTLSv1Adapter, self).__init__()

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize,
                                       block=block, ssl_version=latest_available_ssl_version(),
                                       assert_hostname=self._assert_hostname,
                                       assert_fingerprint=self._assert_fingerprint)


class PixelatedHTTPError(IOError):
    """A HTTP error occurred."""

    def __init__(self, *args, **kwargs):
        """ Initializes HTTPError with optional `response` object. """
        self.status_code = kwargs.pop('status_code', None)
        super(IOError, self).__init__(*args, **kwargs)

    def __str__(self):
        return '%d: %s' % (self.status_code, self.message)


class PixelatedNotAvailableHTTPError(PixelatedHTTPError):
    pass


class PixelatedDispatcherClient(object):
    __slots__ = ('_hostname', '_port', '_base_url', '_cacert', '_scheme', '_assert_hostname', '_fingerprint')

    def __init__(self, hostname, port, cacert=True, ssl=True, assert_hostname=VERIFY_HOSTNAME, fingerprint=None):
        self._hostname = hostname
        self._port = port
        self._scheme = 'https' if ssl else 'http'
        self._base_url = '%s://%s:%s' % (self._scheme, hostname, port)
        self._cacert = cacert
        self._assert_hostname = assert_hostname
        self._fingerprint = fingerprint

    def _get(self, path):
        uri = '%s%s' % (self._base_url, path)
        s = requests.Session()
        s.mount('https://', EnforceTLSv1Adapter(assert_fingerprint=self._fingerprint, assert_hostname=self._assert_hostname))
        r = s.get(uri, verify=self._cacert)
        self._raise_error_for_status(r.status_code, r.reason)
        return r.json()

    def _put(self, path, json_data=None):
        uri = '%s%s' % (self._base_url, path)
        if json_data:
            json_data = json.dumps(json_data)

        s = requests.Session()
        s.mount('https://', EnforceTLSv1Adapter(assert_fingerprint=self._fingerprint, assert_hostname=self._assert_hostname))
        r = s.put(uri, data=json_data, headers={'Content-Type': 'application/json'}, verify=self._cacert)

        self._raise_error_for_status(r.status_code, r.reason)
        return r.json()

    def _post(self, path, json_data=None):
        uri = '%s%s' % (self._base_url, path)
        if json_data:
            json_data = json.dumps(json_data)

        s = requests.Session()
        s.mount('https://', EnforceTLSv1Adapter(assert_fingerprint=self._fingerprint, assert_hostname=self._assert_hostname))
        r = s.post(uri, data=json_data, headers={'Content-Type': 'application/json'}, verify=self._cacert)

        self._raise_error_for_status(r.status_code, r.reason)

        return r.json() if r.content else None

    def _raise_error_for_status(self, status_code, reason):
        if 503 == status_code:
            raise PixelatedNotAvailableHTTPError(reason, status_code=503)
        if 400 <= status_code < 600:
            raise PixelatedHTTPError(reason, status_code=status_code)

    def list(self):
        return self._get('/agents').get('agents')

    def get_agent(self, name):
        return self._get('/agents/%s' % name)

    def get_agent_runtime(self, name):
        return self._get('/agents/%s/runtime' % name)

    def start(self, name):
        payload = {'state': 'running'}
        return self._put('/agents/%s/state' % name, json_data=payload)

    def stop(self, name):
        payload = {'state': 'stopped'}
        return self._put('/agents/%s/state' % name, json_data=payload)

    def agent_exists(self, name):
        try:
            self.get_agent(name)
            return True
        except PixelatedHTTPError:
            return False

    def authenticate(self, name, password):
        payload = {'password': password}
        self._post('/agents/%s/authenticate' % name, json_data=payload)

    def add(self, agent_name, password):
        payload = {
            'name': agent_name,
            'password': password
        }
        self._post('/agents', json_data=payload)

    def reset_data(self, agent_name):
        payload = {
            'name': agent_name
        }

        return self._put('/agents/%s/reset_data' % agent_name, json_data=payload)

    def memory_usage(self):
        return self._get('/stats/memory_usage')

    def validate_connection(self, timeout_in_s=DEFAULT_TIMEOUT_IN_S):
        try:
            start = time.time()
            ok = False
            while not ok and (time.time() - start < timeout_in_s):
                try:
                    self.list()
                    ok = True
                except ConnectionError, e:
                    logger.warn(e.message)
                time.sleep(0.5)
            if not ok:
                raise ConnectionError('Failed to connect to manager (%s) within %d seconds' % (self._hostname, timeout_in_s))
        except PixelatedNotAvailableHTTPError:
            pass  # ignore this kind of problem
