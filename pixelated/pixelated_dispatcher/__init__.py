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
import os
import subprocess
import sys
import logging
import daemon

try:
    from daemon.pidfile import TimeoutPIDLockFile
except ImportError:
    from daemon.pidlockfile import TimeoutPIDLockFile
from pixelated.client.cli import Cli
from pixelated.client.dispatcher_api_client import PixelatedDispatcherClient
from pixelated.proxy import DispatcherProxy
from pixelated.manager import SSLConfig, DispatcherManager
from pixelated.common import init_logging, latest_available_ssl_version

import argparse


PID_ACQUIRE_TIMEOUT_IN_S = 1


def is_proxy():
    for arg in sys.argv:
        if arg == 'proxy':
            return True
    return False


def is_manager():
    for arg in sys.argv:
        if arg == 'manager':
            return True
    return False


def filter_args():
    return [arg for arg in sys.argv[1:] if arg not in ['manager', 'proxy']]


def is_cli():
    return not (is_manager() or is_proxy())


def prepare_venv(root_path):
    venv_path = os.path.join(root_path, 'virtualenv')
    script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'create_mailpile_venv.sh')
    subprocess.call([script, venv_path])
    mailpile_path = os.path.join(venv_path, 'bin', 'mailpile')
    return venv_path, mailpile_path


def can_use_pidfile(pidfile):
    pidfile.acquire()
    pidfile.release()


def run_manager():
    parser = argparse.ArgumentParser(description='Multipile', )
    parser.add_argument('-r', '--root_path', help='The rootpath for mailpile')
    parser.add_argument('-m', '--mailpile_bin', help='The mailpile executable', default='mailpile')
    parser.add_argument('-b', '--backend', help='the backend to use', default='fork', choices=['fork', 'docker'])
    parser.add_argument('--bind', help="bind to interface. Default 127.0.0.1", default='127.0.0.1')
    parser.add_argument('--sslcert', help='The SSL certficate to use', default=None)
    parser.add_argument('--sslkey', help='The SSL key to use', default=None)
    parser.add_argument('--debug', help='Set log level to debug', default=False, action='store_true')
    parser.add_argument('--daemon', help='start in daemon mode and put process into background', default=False, action='store_true')
    parser.add_argument('--pidfile', help='path for pid file. By default none is created', default=None)
    parser.add_argument('--log-config', help='Provide a python logging config file', default=None)
    parser.add_argument('--leap-provider', '-lp', help='Specify the LEAP provider this dispatcher will connect to', default='localhost')
    parser.add_argument('--leap-provider-ca', '-lpc', dest='leap_provider_ca', help='Specify the LEAP provider CA to use to validate connections', default=True)
    parser.add_argument('--leap-provider-fingerprint', '-lpf', dest='leap_provider_fingerprint', help='Specify the LEAP provider fingerprint to use to validate connections', default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--mailpile-virtualenv', help='Use specified virtual env for mailpile', default=None)
    group.add_argument('--auto-mailpile-virtualenv', dest='auto_venv', help='Boostrap virtualenv for mailpile', default=False, action='store_true')

    args = parser.parse_args(args=filter_args())

    if args.sslcert:
        ssl_config = SSLConfig(args.sslcert,
                               args.sslkey,
                               latest_available_ssl_version())
    else:
        ssl_config = None

    venv = args.mailpile_virtualenv
    mailpile_bin = args.mailpile_bin

    if args.auto_venv:
        venv, mailpile_bin = prepare_venv(args.root_path)

    if args.root_path is None or not os.path.isdir(args.root_path):
        raise ValueError('root path %s not found!' % args.root_path)

    log_level = logging.DEBUG if args.debug else logging.INFO
    log_config = args.log_config

    provider_ca = args.leap_provider_ca if args.leap_provider_fingerprint is None else False

    manager = DispatcherManager(args.root_path, mailpile_bin, ssl_config, args.leap_provider, mailpile_virtualenv=venv, provider=args.backend, leap_provider_ca=provider_ca, leap_provider_fingerprint=args.leap_provider_fingerprint, bindaddr=args.bind)

    if args.daemon:
        pidfile = TimeoutPIDLockFile(args.pidfile, acquire_timeout=PID_ACQUIRE_TIMEOUT_IN_S) if args.pidfile else None
        can_use_pidfile(pidfile)
        with daemon.DaemonContext(pidfile=pidfile):
            # init logging only after we have spawned the sub process. Otherwise there might be some hickups
            init_logging('manager', level=log_level, config_file=log_config)
            manager.serve_forever()
    else:
        init_logging('manager', level=log_level, config_file=log_config)
        manager.serve_forever()


def run_proxy():
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--manager', help='hostname:port of the manager')
    parser.add_argument('--banner', help='banner file to show on login screen', default='_login_screen_message.html')
    parser.add_argument('--bind', help="interface to bind to (default: 127.0.0.1)", default='127.0.0.1')
    parser.add_argument('--sslcert', help='proxy HTTP server SSL certificate', default=None)
    parser.add_argument('--sslkey', help='proxy HTTP server SSL key', default=None)
    parser.add_argument('--fingerprint', help='pin certificate to fingerprint', default=None)
    parser.add_argument('--disable-verifyhostname', help='disable hostname verification; if fingerprint is specified it gets precedence', dest="verify_hostname", action='store_false', default=None)
    parser.add_argument('--debug', help='set log level to debug and auto reload files', default=False, action='store_true')
    parser.add_argument('--log-config', help='provide a python logging config file', default=None)
    parser.add_argument('--daemon', help='start in daemon mode and put process into background', default=False, action='store_true')
    parser.add_argument('--pidfile', help='path for pid file. By default none is created', default=None)

    args = parser.parse_args(args=filter_args())

    manager_hostname, manager_port = args.manager.split(':')
    certfile = args.sslcert if args.sslcert else None
    keyfile = args.sslkey if args.sslcert else None
    manager_cafile = certfile if args.fingerprint is None else False

    log_level = logging.DEBUG if args.debug else logging.INFO
    log_config = args.log_config

    client = PixelatedDispatcherClient(manager_hostname, manager_port, cacert=manager_cafile, fingerprint=args.fingerprint, assert_hostname=args.verify_hostname)
    client.validate_connection()

    dispatcher = DispatcherProxy(client, bindaddr=args.bind, keyfile=keyfile,
                                 certfile=certfile, banner=args.banner, debug=args.debug)

    if args.daemon:
        pidfile = TimeoutPIDLockFile(args.pidfile, acquire_timeout=PID_ACQUIRE_TIMEOUT_IN_S) if args.pidfile else None
        can_use_pidfile(pidfile)
        with daemon.DaemonContext(pidfile=pidfile):
            # init logging only after we have spawned the sub process. Otherwise there might be some hickups
            init_logging('proxy', level=log_level, config_file=log_config)
            dispatcher.serve_forever()
    else:
        init_logging('proxy', level=log_level, config_file=log_config)
        dispatcher.serve_forever()


def run_cli():
    Cli(args=filter_args()).run()


def main():
    if is_manager():
        run_manager()
    elif is_proxy():
        run_proxy()
    else:
        run_cli()


if __name__ == '__main__':
    main()
