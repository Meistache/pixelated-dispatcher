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
import getpass

from pixelated.client.dispatcher_api_client import PixelatedDispatcherClient, PixelatedHTTPError
import pixelated.server


__author__ = 'fbernitt'

import sys
import argparse


class Cli(object):
    __slots__ = ('_args', '_out')

    DEFAULT_SERVER_PORT = pixelated.server.DEFAULT_PORT

    def __init__(self, args, out=sys.stdout):
        self._args = args
        self._out = out

    def _build_parser(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--server', help='Provide server url (default: localhost:4449')
        parser.add_argument('-k', '--no-check-certificate', help='don\'t validate SSL/TLS certificates', dest='check_cert', action='store_false', default=True)
        parser.add_argument('--no-ssl', help='Force unsecured connection', dest='use_ssl', action='store_false', default=True)
        subparsers = parser.add_subparsers(help='Commands', dest='cmd')
        subparsers.add_parser('list', help='List known agents')
        subparsers.add_parser('running', help='List known agents')
        addparser = subparsers.add_parser('add', help='Add a agent')
        addparser.add_argument('name', help='name of new user')
        startparser = subparsers.add_parser('start', help='Start agent')
        startparser.add_argument('name', help='name of user')
        stopparser = subparsers.add_parser('stop', help='Stop agent')
        stopparser.add_argument('name', help='name of user')
        infoparser = subparsers.add_parser('info', help='Show agent info')
        infoparser.add_argument('name', help='name of user')
        subparsers.add_parser('memory_usage', help='show memory usage')
        return parser

    def run(self):
        parser = self._build_parser()

        try:
            args = parser.parse_args(self._args)
            if args.server:
                host, port = args.server.split(':')
            else:
                host, port = 'localhost', Cli.DEFAULT_SERVER_PORT

            cli = self._create_cli(host, port, args.check_cert, args.use_ssl)
            if 'list' == args.cmd:
                for agent in cli.list():
                    self._out.write('%s\n' % agent['name'])
            elif 'running' == args.cmd:
                for agent in cli.list():
                    if 'running' == agent['state']:
                        self._out.write('%s\n' % agent['name'])
            elif 'add' == args.cmd:
                name = args.name
                password = getpass.getpass('Enter password for new user', self._out)
                cli.add(name, password)
            elif 'start' == args.cmd:
                name = args.name
                cli.start(name)
            elif 'stop' == args.cmd:
                name = args.name
                cli.stop(name)
            elif 'info' == args.cmd:
                name = args.name
                info = cli.get_agent_runtime(name)
                self._out.write('port:\t%s\n' % info['port'])
            elif 'memory_usage' == args.cmd:
                usage = cli.memory_usage()
                self._out.write('memory usage:\t%d\n' % usage['total_usage'])
                self._out.write('average usage:\t%d\n\n' % usage['average_usage'])
                for agent in usage['agents']:
                    self._out.write('\t%s:\t%d\n' % (agent['name'], agent['memory_usage']))
        except PixelatedHTTPError, e:
            sys.stderr.write('%s\n' % str(e))
            sys.exit(1)

        except SystemExit:
            pass

    def _create_cli(self, host, port, cacert, ssl):
        return PixelatedDispatcherClient(host, port, cacert=cacert, ssl=ssl)