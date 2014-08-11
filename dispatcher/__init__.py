#
# Copyright 2014 ThoughtWorks Deutschland GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import traceback
import sys

from tornado import web
from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer

from client.multipile import PixelatedHTTPError


__author__ = 'fbernitt'

import os
import tornado.ioloop
import tornado.web
import tornado.escape


COOKIE_NAME = 'pixelated_user'


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        cookie = self.get_secure_cookie(COOKIE_NAME)
        if cookie:
            return tornado.escape.json_decode(cookie)
        else:
            return None


class MainHandler(BaseHandler):
    __slots__ = '_client'

    def initialize(self, client):
        self._client = client

    @tornado.web.authenticated
    @tornado.web.asynchronous
    def get(self):
        runtime = self._client.get_agent_runtime(self.current_user)
        if runtime['state'] == 'running':
            port = runtime['port']
            self.forward(port, '127.0.0.1')
        else:
            self.set_status(503)
            self.write('Sorry, your agent is down')
            self.finish()

    def handle_response(self, response):
        if response.error and not isinstance(response.error, tornado.httpclient.HTTPError):
            self.set_status(500)
            self.write("Internal server error:\n" + str(response.error))
            self.finish()
        else:
            self.set_status(response.code)
            for header in ("Date", "Cache-Control", "Server", "Content-Type", "Location"):
                v = response.headers.get(header)
                if v:
                    self.set_header(header, v)
            if response.body:
                self.write(response.body)
            self.finish()

    def forward(self, port=None, host=None):
        try:
            tornado.httpclient.AsyncHTTPClient().fetch(
                tornado.httpclient.HTTPRequest(
                    url="%s://%s:%s%s" % (
                        'http', host or "127.0.0.1", port or 80, self.request.uri),
                    method=self.request.method,
                    body=None if not self.request.body else self.request.body,
                    headers=self.request.headers,
                    follow_redirects=False,
                    request_timeout=1),
                self.handle_response)
        except tornado.httpclient.HTTPError, x:
            if hasattr(x, 'response') and x.response:
                self.handle_response(x.response)
        except:
            self.set_status(500)
            self.write("Internal server error:\n" + ''.join(traceback.format_exception(*sys.exc_info())))
            self.finish()


class AuthLoginHandler(tornado.web.RequestHandler):
    def initialize(self, client):
        self._client = client

    def get(self):
        self.render('login.html')

    def post(self):

        username = self.get_argument("username", "")
        password = self.get_argument("password", "")

        try:
            agent = self._client.get_agent(username)

            # no authenticate with server...
            self._client.authenticate(username, password)
            self.set_current_user(username)
            self.redirect(u'/')
        except PixelatedHTTPError:
            self.redirect(u'/auth/login?error=%s' % tornado.escape.url_escape('Invalid credentials'))

    def set_current_user(self, username):
        if username:
            self.set_secure_cookie(COOKIE_NAME, tornado.escape.json_encode(username))
        else:
            self.clear_cookie(COOKIE_NAME)


class AuthLogoutHandler(tornado.web.RequestHandler):
    def get(self):
        self.clear_cookie(COOKIE_NAME)
        self.write("You are now logged out")


class Dispatcher(object):
    __slots__ = ('_port', '_client', '_bindaddr', '_ioloop', '_certfile', '_keyfile', '_server')

    def __init__(self, dispatcher_client, bindaddr='127.0.0.1', port=8080, certfile=None, keyfile=None):
        self._port = port
        self._client = dispatcher_client
        self._bindaddr = bindaddr
        self._certfile = certfile
        self._keyfile = keyfile
        self._ioloop = None
        self._server = None

    def create_app(self):
        app = tornado.web.Application(
            [
                (r"/auth/login", AuthLoginHandler, dict(client=self._client)),
                (r"/auth/logout", AuthLogoutHandler),
                (r"/dispatcher_static/", web.StaticFileHandler),
                (r"/.*", MainHandler, dict(client=self._client))
            ],
            cookie_secret='quwoqwjladsfasdlfjqsdojqwojqofdlsfasofhqwo0qoqsflasdnfaslfjo0324rsd',
            login_url='/auth/login',
            template_path=os.path.join(os.path.dirname(__file__), '..', 'files', "templates"),
            static_path=os.path.join(os.path.dirname(__file__), '..', 'files', "static"),
            static_url_prefix='/dispatcher_static/',  # needs to be bound to a different prefix as agent uses static
            xsrf_cookies=True,
            debug=True)
        return app

    @property
    def ssl_options(self):
        if self._certfile:
            return {
                "certfile": os.path.join(self._certfile),
                "keyfile": os.path.join(self._keyfile),
            }
        else:
            return None

    def serve_forever(self):
        app = self.create_app()
        # app.listen(port=self._port, address=self._bindaddr, ssl_options=self.ssl_options)
        self._server = HTTPServer(app, ssl_options=self.ssl_options)
        self._server.listen(port=self._port, address=self._bindaddr)
        self._ioloop = tornado.ioloop.IOLoop.instance()
        self._ioloop.start()  # this is a blocking call, server has stopped on next line
        self._ioloop = None

    def shutdown(self):
        if self._ioloop:
            self._server.stop()
            self._ioloop.stop()