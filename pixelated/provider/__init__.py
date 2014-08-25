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


class NotEnoughFreeMemory(Exception):
    pass


class Provider(object):  # pragma: no cover
    def initialize(self):
        pass

    def add(self, name, password):
        pass

    def remove(self, name):
        pass

    def list(self):
        pass

    def list_running(self):
        pass

    def start(self, name):
        pass

    def stop(self, name):
        pass

    def status(self, name):
        pass

    def authenticate(self, name, password):
        pass

    def memory_usage(self):
        pass
