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
import stat
import socket
from os.path import join, isdir, isfile, exists
from tempfile import NamedTemporaryFile
from time import sleep, clock
from mock import patch, MagicMock
import pkg_resources
import requests
import json
import shutil
from tempdir import TempDir
from psutil._common import pmem
from threading import Thread
from pixelated.provider.base_provider import ProviderInitializingException
from pixelated.provider.docker import DockerProvider, CredentialsToDockerStdinWriter, DOCKER_API_VERSION
from pixelated.provider.docker.pixelated_adapter import PixelatedDockerAdapter
from pixelated.test.util import StringIOMatcher
from pixelated.exceptions import *
from pixelated.users import UserConfig, Users
from pixelated.bitmask_libraries.leap_config import LeapProviderX509Info

import unittest


class CredentialsToDockerStdinWriterTest(unittest.TestCase):
    @patch('pixelated.provider.docker.multiprocessing.Process')
    def test_starts_background_process(self, process_mock):
        cw = CredentialsToDockerStdinWriter('some docker url', 'container_id', 'leap provider hostname', 'username', 'password')

        cw.start()

        process_mock.assert_called_with(target=cw.run)
        self.assertTrue(process_mock.return_value.daemon)
        process_mock.return_value.start.assert_called_once_with()

    @patch('pixelated.provider.docker.docker.Client')
    def test_run_sends_credentials_to_docker(self, docker_mock):
        # given
        socket_mock = MagicMock()
        client = docker_mock.return_value
        client.attach_socket.return_value = socket_mock
        cw = CredentialsToDockerStdinWriter('some docker url', 'container_id', 'leap provider hostname', 'username', 'password')

        # when
        cw.run()

        # then
        docker_mock.assert_called_once_with(base_url='some docker url', version=DOCKER_API_VERSION)
        expected_params = {
            'stdin': True,
            'stderr': False,
            'stream': True,
            'stdout': False
        }
        client.attach_socket.assert_called_once_with(container='container_id', params=expected_params)
        socket_mock.send.assert_called_once_with('{"password": "password", "leap_provider_hostname": "leap provider hostname", "user": "username"}\n')
        socket_mock.shutdown.assert_called_once_with(socket.SHUT_WR)
        socket_mock.close.assert_called_once_with()

    def test_terminate_terminates_process(self):
        cw = CredentialsToDockerStdinWriter('some docker url', 'container_id', 'leap provider hostname', 'username', 'password')
        cw._process = MagicMock()

        cw.terminate()

        cw._process.terminate.assert_called_once_with()


class DockerProviderTest(unittest.TestCase):
    def setUp(self):
        self._provider_hostname = 'example.org'
        self.users = MagicMock(spec=Users)
        self._tmpdir = TempDir()
        self.root_path = self._tmpdir.name
        self._adapter = MagicMock(wraps=PixelatedDockerAdapter(self._provider_hostname))
        self._adapter.docker_image_name.return_value = 'pixelated'
        self._leap_provider_x509 = LeapProviderX509Info()

    def tearDown(self):
        self._tmpdir.dissolve()

    def test_that_docker_api_version_is_pinned_to_v1_14(self):
        self.assertEqual('1.14', DOCKER_API_VERSION)

    @patch('pixelated.provider.docker.docker.Client')
    def test_constructor_expects_docker_url(self, docker_mock):
        DockerProvider(self.root_path, self._adapter, self._leap_provider_x509, 'some docker url')

    @patch('pixelated.provider.docker.docker.Client')
    def test_initialize_builds_docker_image(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = []
        dockerfile = pkg_resources.resource_string('pixelated.resources', 'Dockerfile.pixelated')

        # when
        DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509, 'some docker url').initialize()

        # then
        docker_mock.assert_called_once_with(base_url="some docker url", version=DOCKER_API_VERSION)
        client.build.assert_called_once_with(path=None, fileobj=StringIOMatcher(dockerfile), tag='pixelated:latest')

    @patch('pixelated.provider.docker.docker.Client')
    def test_initialize_downloads_docker_image_if_image_name_contains_slash(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = []
        self._adapter.docker_image_name.return_value = 'pixelated/pixelated-user-agent'

        # when
        DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509, 'some docker url').initialize()
        # then
        docker_mock.assert_called_once_with(base_url='some docker url', version=DOCKER_API_VERSION)
        client.pull.assert_called_with(tag='latest', repository='pixelated/pixelated-user-agent', stream=True)

    @patch('pixelated.provider.docker.docker.Client')
    def test_initialize_downloads_and_starts_logger_docker_image_if_not_yet_available(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = []
        container = {'Id': 'some id'}
        client.create_container.return_value = container
        expected_syslog_tag = '.user_agent'

        # when
        DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509, 'some docker url').initialize()

        # then
        docker_mock.assert_called_once_with(base_url='some docker url', version=DOCKER_API_VERSION)
        client.pull.assert_called_with(tag='latest', repository='pixelated/logspout', stream=True)
        client.create_container.assert_called_once_with(
            image='pixelated/logspout:latest',
            command='syslog://localhost:514?append_tag=%s' % expected_syslog_tag,
            volumes='/tmp/docker.sock',
            environment={'HTTP_PORT': '51957'})
        client.start.assert_called_once_with(
            container='some id',
            network_mode='host',
            binds={'/var/run/docker.sock': {'bind': '/tmp/docker.sock', 'ro': False}})

    @patch('pixelated.provider.docker.docker.Client')
    def test_initialize_skips_image_build_or_download_if_already_available(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = [
            {'Created': 1404833111,
             'VirtualSize': 297017244,
             'ParentId': '57885511c8444c2b89743bef8b89eccb65f302b2a95daa95dfcc9b972807b6db',
             'RepoTags': ['pixelated:latest'],
             'Id': 'b4f10a2395ab8dfc5e1c0fae26fa56c7f5d2541debe54263105fe5af1d263189',
             'Size': 181956643}]
        provider = DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509)

        # when
        provider.initialize()

        # then
        self.assertFalse(client.build.called)
        self.assertFalse(provider.initializing)

    @patch('pixelated.provider.docker.docker.Client')
    def test_initialize_doesnt_download_logger_image_if_already_available(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = [
            {'Created': 1404833111,
             'VirtualSize': 297017244,
             'ParentId': '57885511c8444c2b89743bef8b89eccb65f302b2a95daa95dfcc9b972807b6db',
             'RepoTags': ['pixelated/logspout:latest'],
             'Id': 'b4f10a2395ab8dfc5e1c0fae26fa56c7f5d2541debe54263105fe5af1d263189', 'Size': 181956643}]

        # when
        DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509, 'some docker url').initialize()

        # then
        docker_mock.assert_called_once_with(base_url='some docker url', version=DOCKER_API_VERSION)
        client.pull.assert_never_called_with(tag='latest', repository='pixelated/logspout', stream=True)

    @patch('pixelated.provider.docker.docker.Client')
    def test_reports_initializing_while_initialize_is_running(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = []

        def build(path, fileobj, tag):
            sleep(0.2)
            return []

        client.build.side_effect = build
        provider = DockerProvider(self._adapter, 'some provider', self._leap_provider_x509, 'some docker url')

        self.assertTrue(provider.initializing)

        # when
        t = Thread(target=provider.initialize)  # move to thread so that initializing behaviour is observable
        t.start()

        # then
        sleep(0.1)
        self.assertTrue(provider.initializing)
        t.join()
        self.assertFalse(provider.initializing)

    @patch('pixelated.provider.docker.docker.Client')
    def test_reports_initializing_while_initialize_is_running_and_image_downloaded(self, docker_mock):
        # given
        client = docker_mock.return_value
        client.images.return_value = []
        self._adapter.docker_image_name.return_value = 'pixelated/pixelated-user-agent'

        def download(repository, tag, stream):
            sleep(0.2)
            return []

        client.pull.side_effect = download
        provider = DockerProvider(self._adapter, 'some provider', self._leap_provider_x509, 'some docker url')

        self.assertTrue(provider.initializing)

        # when
        t = Thread(target=provider.initialize)  # move to thread so that initializing behaviour is observable
        t.start()

        # then
        sleep(0.1)
        self.assertTrue(provider.initializing)
        t.join()
        self.assertFalse(provider.initializing)

    @patch('pixelated.provider.docker.docker.Client')
    def test_throws_initializing_exception_while_initializing(self, docker_mock):
        # given
        provider = DockerProvider(self._adapter, 'provider url', self._leap_provider_x509, 'some docker url')

        # when/then
        self.assertRaises(ProviderInitializingException, provider.start, 'test')
        self.assertRaises(ProviderInitializingException, provider.remove, 'test')
        self.assertRaises(ProviderInitializingException, provider.list_running)
        self.assertRaises(ProviderInitializingException, provider.stop, 'test')
        self.assertRaises(ProviderInitializingException, provider.status, 'test')
        self.assertRaises(ProviderInitializingException, provider.memory_usage)

    @patch('pixelated.provider.docker.docker.Client')
    def test_that_instance_can_be_started(self, docker_mock):
        expected_extra_hosts = {'nicknym.example.tld': '172.17.42.1', 'pixelated.example.tld': '172.17.42.1', 'api.example.tld': '172.17.42.1', 'example.tld': '172.17.42.1'}
        uid = os.getuid()
        self._adapter.docker_image_name.return_value = 'pixelated/pixelated-user-agent'
        client = docker_mock.return_value
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        prepare_pixelated_container = MagicMock()
        container = MagicMock()
        client.create_container.side_effect = [prepare_pixelated_container, container]
        client.wait.return_value = 0
        self._leap_provider_x509.ca_bundle = 'some ca bundle'

        with patch('pixelated.provider.docker.socket.getfqdn') as mock:
            mock.return_value = 'pixelated.example.tld'
            provider.start(self._user_config('test'))

        client.create_container.assert_any_call('pixelated/pixelated-user-agent', '/bin/bash -l -c "/usr/bin/pixelated-user-agent --leap-home /mnt/user --host 0.0.0.0 --port 4567 --organization-mode --leap-provider-cert /mnt/user/dispatcher-leap-provider-ca.crt"', mem_limit='300m', user=uid, name='test', volumes=['/mnt/user'], ports=[4567], environment={'DISPATCHER_LOGOUT_URL': '/auth/logout', 'FEEDBACK_URL': 'https://example.org/tickets'}, stdin_open=True)
        client.create_container.assert_any_call('pixelated/pixelated-user-agent', '/bin/true', name='pixelated_prepare', volumes=['/mnt/user'], environment={'DISPATCHER_LOGOUT_URL': '/auth/logout', 'FEEDBACK_URL': 'https://example.org/tickets'})

        data_path = join(self.root_path, 'test', 'data')

        client.start.assert_any_call(container, binds={data_path: {'bind': '/mnt/user', 'ro': False}}, port_bindings={4567: ('127.0.0.1', 5000)}, extra_hosts=expected_extra_hosts)
        client.start.assert_any_call(prepare_pixelated_container, binds={data_path: {'bind': '/mnt/user', 'ro': False}})

    @patch('pixelated.provider.docker.docker.Client')
    def test_that_existing_container_gets_reused(self, docker_mock):
        client = docker_mock.return_value
        client.containers.side_effect = [[], [{u'Status': u'Exited (-1) About an hour ago', u'Created': 1405332375, u'Image': u'pixelated:latest', u'Ports': [], u'Command': u'/bin/bash -l -c "/usr/bin/pixelated-user-agent --dispatcher"', u'Names': [u'/test'], u'Id': u'adfd4633fc42734665d7d98076b19b5f439648678b3b76db891f9d5072af50b6'}]]
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        container = MagicMock()
        client.create_container.return_value = container

        provider.start(self._user_config('test'))

        client.containers.assert_called_with(all=True)
        self.assertFalse(client.build.called)

    @patch('pixelated.provider.docker.docker.Client')
    def test_running_containers_empty_if_none_started(self, docker_mock):
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        running = provider.list_running()

        self.assertEqual([], running)

    @patch('pixelated.provider.docker.docker.Client')
    def test_running_returns_running_container(self, docker_mock):
        client = docker_mock.return_value
        client.containers.side_effect = [[], [], [{u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}]]
        client.wait.return_value = 0
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        provider.start(self._user_config('test'))

        running = provider.list_running()

        self.assertEqual(['test'], running)

    @patch('pixelated.provider.docker.docker.Client')
    def test_a_container_cannot_be_started_twice(self, docker_mock):
        client = docker_mock.return_value
        client.containers.side_effect = [[], [], [{u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}]]
        client.wait.return_value = 0
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        user_config = self._user_config('test')
        provider.start(user_config)

        self.assertRaises(InstanceAlreadyRunningError, provider.start, user_config)

    @patch('pixelated.provider.docker.docker.Client')
    def test_stopping_not_running_container_raises_value_error(self, docker_mock):
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        self.assertRaises(InstanceNotRunningError, provider.stop, 'test')

    @patch('pixelated.provider.docker.docker.Client')
    def test_stop_running_container(self, docker_mock):
        # given
        user_config = self._user_config('test')
        client = docker_mock.return_value
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 4567}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        client.containers.side_effect = [[], [], [container], [container], [container]]
        client.wait.return_value = 0
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        provider.pass_credentials_to_agent(user_config, 'test')
        provider.start(user_config)

        # when
        provider.stop('test')

        # then
        client.stop.assert_called_once_with(container, timeout=10)
        self.assertFalse(5000 in provider._used_ports())
        self.assertTrue('test' not in provider._credentials)

    @patch('pixelated.provider.docker.docker.Client')
    def test_stop_running_container_calls_kill_if_stop_times_out(self, docker_mock):
        # given
        client = docker_mock.return_value
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 4567}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        client.containers.side_effect = [[], [], [container], [container], [container]]
        client.wait.return_value = 0
        client.stop.side_effect = requests.exceptions.Timeout

        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        provider.start(self._user_config('test'))

        # when
        provider.stop('test')

        # then
        client.stop.assert_called_once_with(container, timeout=10)
        client.kill.assert_called_once_with(container)

    @patch('pixelated.provider.docker.docker.Client')
    def test_status_stopped(self, docker_mock):
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        self.assertEqual({'state': 'stopped'}, provider.status('test'))

    @patch('pixelated.provider.docker.docker.Client')
    def test_status_running(self, docker_mock):
        client = docker_mock.return_value
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 33144}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        client.containers.side_effect = [[], [], [container], [container]]
        client.wait.return_value = 0
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        provider.start(self._user_config('test'))

        self.assertEqual({'state': 'running', 'port': 5000}, provider.status('test'))

    @patch('pixelated.provider.docker.Process')
    @patch('pixelated.provider.docker.docker.Client')
    def test_memory_usage(self, docker_mock, process_mock):
        # given
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 33144}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        info = {u'HostsPath': u'/var/lib/docker/containers/f2cdb04277e9e056c610240edffe8ff94ae272e462312c270e5300975d60af89/hosts', u'Created': u'2014-07-14T13:17:46.17558664Z', u'Image': u'f63df19194389be6481a174b36d291c483c8982d5c07485baa71a46b7f6582c8', u'Args': [], u'Driver': u'aufs', u'HostConfig': {u'PortBindings': {u'4567/tcp': [{u'HostPort': u'5000', u'HostIp': u'0.0.0.0'}]}, u'NetworkMode': u'', u'Links': None, u'LxcConf': None, u'ContainerIDFile': u'', u'Binds': [u'/tmp/multipile/folker:/mnt/user:rw'], u'PublishAllPorts': False, u'Dns': None, u'DnsSearch': None, u'Privileged': False, u'VolumesFrom': None}, u'MountLabel': u'', u'VolumesRW': {u'/mnt/user': True}, u'State': {u'Pid': 3250, u'Paused': False, u'Running': True, u'FinishedAt': u'0001-01-01T00:00:00Z', u'StartedAt': u'2014-07-14T13:17:46.601922899Z', u'ExitCode': 0}, u'ExecDriver': u'native-0.2', u'ResolvConfPath': u'/etc/resolv.conf', u'Volumes': {u'/mnt/user': u'/tmp/multipile/folker'}, u'Path': u'/bin/bash -l -c "/usr/bin/pixelated-user-agent --dispatcher"', u'HostnamePath': u'/var/lib/docker/containers/f2cdb04277e9e056c610240edffe8ff94ae272e462312c270e5300975d60af89/hostname', u'ProcessLabel': u'', u'Config': {u'MemorySwap': 0, u'Hostname': u'f2cdb04277e9', u'Entrypoint': None, u'PortSpecs': None, u'Memory': 0, u'OnBuild': None, u'OpenStdin': False, u'Cpuset': u'', u'Env': [u'HOME=/', u'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'], u'User': u'', u'CpuShares': 0, u'AttachStdout': True, u'NetworkDisabled': False, u'WorkingDir': u'', u'Cmd': [u'/bin/bash -l -c "/usr/bin/pixelated-user-agent --dispatcher"'], u'StdinOnce': False, u'AttachStdin': False, u'Volumes': {u'/mnt/user': {}}, u'Tty': False, u'AttachStderr': True, u'Domainname': u'', u'Image': u'pixelated', u'ExposedPorts': {u'4567/tcp': {}}}, u'Id': u'f2cdb04277e9e056c610240edffe8ff94ae272e462312c270e5300975d60af89', u'NetworkSettings': {u'Bridge': u'docker0', u'PortMapping': None, u'Gateway': u'172.17.42.1', u'IPPrefixLen': 16, u'IPAddress': u'172.17.0.14', u'Ports': {u'4567/tcp': [{u'HostPort': u'5000', u'HostIp': u'0.0.0.0'}]}}, u'Name': u'/folker'}
        client = docker_mock.return_value
        client.containers.return_value = [container]
        client.inspect_container.return_value = info

        psutil_mock = process_mock.return_value
        psutil_mock.memory_info.return_value = pmem(1024, 2048)

        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        # when
        usage = provider.memory_usage()

        # then
        self.assertEqual({'total_usage': 1024,
                          'average_usage': 1024,
                          'agents': [
                              {'name': 'test', 'memory_usage': 1024}
                          ]}, usage)

    @patch('pixelated.provider.docker.docker.Client')
    def test_remove_error_if_not_exist(self, docker_mock):
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        self.assertRaises(ValueError, provider.remove, self._user_config('does_not_exist'))

    @patch('pixelated.provider.docker.docker.Client')
    def test_remove(self, docker_mock):
        # given
        user_config = self._user_config('test')
        os.makedirs(join(user_config.path, 'data'))
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        # when
        provider.remove(user_config)

        # then
        self.assertTrue(exists(user_config.path))
        self.assertFalse(exists(join(user_config.path, 'data')))

    @patch('pixelated.provider.docker.docker.Client')
    def test_cannot_remove_while_running(self, docker_mock):
        # given
        client = docker_mock.return_value
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 4567}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        client.containers.side_effect = [[], [], [container]]
        client.wait.return_value = 0

        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        user_config = self._user_config('test')
        provider.start(user_config)

        # when/then
        self.assertRaises(ValueError, provider.remove, user_config)

    @patch('pixelated.provider.docker.docker.Client')
    def test_reset_data(self, docker_mock):
        # given
        user_config = self._user_config('test')
        os.makedirs(join(user_config.path, 'data'))
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        # when
        provider.reset_data(user_config)

        # then
        self.assertTrue(exists(user_config.path))
        self.assertFalse(exists(join(user_config.path, 'data')))

    @patch('pixelated.provider.docker.docker.Client')
    def test_reset_data_does_not_complain_if_there_is_no_data(self, docker_mock):
        # given
        user_config = self._user_config('test')
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        # when
        provider.reset_data(user_config)

        # then
        self.assertTrue(exists(user_config.path))
        self.assertFalse(exists(join(user_config.path, 'data')))

    @patch('pixelated.provider.docker.docker.Client')
    def test_reset_data_fails_if_user_does_not_exist(self, docker_mock):
        # given
        user_config = self._user_config('test')
        shutil.rmtree(user_config.path)
        client = docker_mock.return_value
        client.containers.return_value = []
        provider = self._create_initialized_provider(self._adapter, 'some docker url')

        # when/then
        self.assertRaises(ValueError, provider.reset_data, user_config)

    @patch('pixelated.provider.docker.docker.Client')
    def test_reset_data_fails_if_agent_is_running(self, docker_mock):
        # given
        client = docker_mock.return_value
        container = {u'Status': u'Up 20 seconds', u'Created': 1404904929, u'Image': u'pixelated:latest', u'Ports': [{u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 5000, u'PrivatePort': 4567}], u'Command': u'sleep 100', u'Names': [u'/test'], u'Id': u'f59ee32d2022b1ab17eef608d2cd617b7c086492164b8c411f1cbcf9bfef0d87'}
        client.containers.side_effect = [[], [], [container]]
        client.wait.return_value = 0

        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        user_config = self._user_config('test')
        provider.start(user_config)

        # when/then
        self.assertRaises(InstanceAlreadyRunningError, provider.reset_data, user_config)

    @patch('pixelated.provider.docker.TempDir')
    @patch('pixelated.provider.docker.pkg_resources')
    @patch('pixelated.provider.docker.docker.Client')
    def test_use_build_script_instead_of_docker_file_if_available(self, docker_mock, res_mock, tempDir_mock):
        # given
        provider = DockerProvider(self._adapter, 'leap_provider', self._leap_provider_x509)

        tempBuildDir = TempDir()
        try:
            tempDir_mock.return_value = tempBuildDir
            tempBuildDir_name = tempBuildDir.name
            with NamedTemporaryFile() as file:
                res_mock.resource_exists.return_value = True
                res_mock.resource_string.return_value = '#!/bin/bash\necho %s $PWD > %s' % (file.name, file.name)

                # when
                provider.initialize()

                # then
                res_mock.resource_exists.assert_called_with('pixelated.resources', 'init-pixelated-docker-context.sh')
                res_mock.resource_string.assert_called_with('pixelated.resources', 'init-pixelated-docker-context.sh')
                with open(file.name, "r") as input:
                    data = input.read().replace('\n', '')
                    self.assertEqual('%s %s' % (file.name, os.path.realpath(tempBuildDir_name)), data)

                docker_mock.return_value.build.assert_called_once_with(path=tempBuildDir_name, tag='pixelated:latest', fileobj=None)
        finally:
            tempBuildDir.dissolve()

    @patch('pixelated.provider.docker.docker.Client')
    @patch('pixelated.provider.docker.CredentialsToDockerStdinWriter')
    def test_that_credentials_are_passed_to_agent_by_stdin(self, credentials_mock, docker_mock):
        # given
        user_config = self._user_config('test')
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        prepare_pixelated_container = MagicMock()
        container = MagicMock()

        class ProcessStub(object):
            def start(self):
                self._target()

            def __init__(self, target):
                self._target = target

        client = docker_mock.return_value
        client.create_container.side_effect = [prepare_pixelated_container, container]
        client.wait.return_value = 0

        # when
        provider.pass_credentials_to_agent(user_config, 'password')

        provider.start(user_config)

        # then
        credentials_mock.return_value.start.assert_called_once_with()

    @patch('pixelated.provider.docker.docker.Client')
    def test_provider_checks_working_connection_to_docker(self, docker_mock):
        client = docker_mock.return_value
        client.info.side_effect = Exception

        self.assertRaises(Exception, DockerProvider, self._adapter, 'leap_provider', self._leap_provider_x509)

    @patch('pixelated.provider.docker.docker.Client')
    def test_that_provider_x509_ca_bundle_is_copied_to_agent(self, docker_mock):
        user_config = self._user_config('test')
        provider = self._create_initialized_provider(self._adapter, 'some docker url')
        client = docker_mock.return_value
        client.wait.return_value = 0

        with NamedTemporaryFile() as ca_file:
            with open(ca_file.name, 'w') as fd:
                fd.write('some certificate')
            self._leap_provider_x509.ca_bundle = ca_file.name

            provider.start(user_config)

            self.assertTrue(exists(join(self.root_path, 'test', 'data', 'dispatcher-leap-provider-ca.crt')))

    def _create_initialized_provider(self, adapter, docker_url=DockerProvider.DEFAULT_DOCKER_URL):
        provider = DockerProvider(adapter, 'leap_provider_hostname', self._leap_provider_x509, docker_url)
        provider._initializing = False
        return provider

    def _user_config(self, name):
        path = join(self.root_path, name)
        os.makedirs(path)
        return UserConfig(name, path)
