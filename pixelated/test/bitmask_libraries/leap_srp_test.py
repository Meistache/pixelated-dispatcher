#
# Copyright (c) 2014 ThoughtWorks, Inc.
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
import unittest
import binascii
from urlparse import parse_qs
from mock import MagicMock, patch, ANY
from requests import Session

from httmock import urlmatch, all_requests, HTTMock, response
from requests.exceptions import Timeout
import srp
from pixelated.bitmask_libraries.leap_srp import LeapSecureRemotePassword, LeapAuthException, LeapSRPTLSConfig


(salt_bytes, verification_key_bytes) = srp.create_salted_verification_key('username', 'password', hash_alg=srp.SHA256, ng_type=srp.NG_1024)
verifier = None


@all_requests
def not_found_mock(url, request):
    return {'status_code': 404,
            'content': 'foobar'}


@all_requests
def timeout_mock(url, request):
    raise Timeout()


@urlmatch(netloc=r'(.*\.)?leap\.local$')
def srp_login_server_simulator_mock(url, request):
    global verifier

    data = parse_qs(request.body)
    if 'login' in data:
        # SRP Authentication Step 1
        A = binascii.unhexlify(data.get('A')[0])

        verifier = srp.Verifier('username', salt_bytes, verification_key_bytes, A, hash_alg=srp.SHA256, ng_type=srp.NG_1024)
        (salt, B) = verifier.get_challenge()

        content = {
            'salt': binascii.hexlify(salt),
            'B': binascii.hexlify(B)
        }

        return {'status_code': 200,
                'content': json.dumps(content)}

    else:
        # SRP Authentication Step 2
        data = parse_qs(request.body)
        client_auth = binascii.unhexlify(data.get('client_auth')[0])

        M2 = verifier.verify_session(client_auth)

        if not verifier.authenticated():
            return {'status_code': 404,
                    'content': ''}

        content = {
            'M2': binascii.hexlify(M2),
            'id': 'some id',
            'token': 'some token'
        }
        headers = {
            'Content-Type': 'application/json',
            'Set-Cookie': '_session_id=some_session_id;'}
        return response(200, content, headers, None, 5, request)


class LeapSRPTest(unittest.TestCase):

    def test_status_code_is_checked(self):
        with HTTMock(not_found_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.authenticate, 'https://api.leap.local', 'username', 'password')

    def test_invalid_username(self):
        with HTTMock(srp_login_server_simulator_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.authenticate, 'https://api.leap.local', 'invalid_user', 'password')

    def test_invalid_password(self):
        with HTTMock(srp_login_server_simulator_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.authenticate, 'https://api.leap.local', 'username', 'invalid')

    def test_login(self):
        with HTTMock(srp_login_server_simulator_mock):
            lsrp = LeapSecureRemotePassword()
            leap_session = lsrp.authenticate('https://api.leap.local', 'username', 'password')

            self.assertIsNotNone(leap_session)
            self.assertEqual('username', leap_session.user_name)
            self.assertEqual('1', leap_session.api_version)
            self.assertEqual('https://api.leap.local', leap_session.api_server_name)
            self.assertEqual('some token', leap_session.token)
            self.assertEqual('some_session_id', leap_session.session_id)

    def test_timeout(self):
        with HTTMock(timeout_mock):
            lrsp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lrsp.authenticate, 'https://api.leap.local', 'username', 'password')

    def test_register_raises_auth_exception_on_error(self):
        with HTTMock(not_found_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.register, 'https://api.leap.local', 'username', 'password')

    def test_register(self):
        @urlmatch(netloc=r'(.*\.)?leap\.local$', path='/1/users')
        def register_success(url, request):

            content = {
                'login': 'username',
                'ok': True
            }

            return {'status_code': 201,
                    'content': content}

        with HTTMock(register_success, not_found_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertTrue(lsrp.register('https://api.leap.local', 'username', 'password'))

    def test_register_user_exists(self):
        @urlmatch(netloc=r'(.*\.)?leap\.local$', path='/1/users')
        def register_error_user_exists(url, request):
            content = {"errors": {
                "login": [
                    "has already been taken", "has already been taken", "has already been taken"
                ]}}

            return {'status_code': 422,
                    'content': content}

        with HTTMock(register_error_user_exists, not_found_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.register, 'https://api.leap.local', 'username', 'password')

    def test_registration_timeout(self):
        with HTTMock(timeout_mock):
            lsrp = LeapSecureRemotePassword()
            self.assertRaises(LeapAuthException, lsrp.register, 'https://api.leap.local', 'username', 'password')

    def test_specify_tls_config(self):
        tls_config = LeapSRPTLSConfig(ca_bundle=None, assert_hostname='hostname', assert_fingerprint='fingerprint')

        with HTTMock(srp_login_server_simulator_mock):
            session = Session()
            session_mock = MagicMock(wraps=session)
            with patch('pixelated.bitmask_libraries.leap_srp.Session', return_value=session_mock):
                lsrp = LeapSecureRemotePassword(tls_config=tls_config)
                lsrp.authenticate('https://api.leap.local', 'username', 'password')

            session_mock.mount.assert_called_once_with('https://', ANY)
            adapter = session_mock.mount.call_args[0][1]
            self.assertEqual('hostname', adapter._assert_hostname)
            self.assertEqual('fingerprint', adapter._assert_fingerprint)
