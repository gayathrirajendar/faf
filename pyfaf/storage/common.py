# Copyright (C) 2012 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from . import Column
from . import GenericTable
from . import Integer
from . import String

class DbMd(GenericTable):
    __tablename__ = "_dbmd"

    __columns__ = [ Column("version", Integer, primary_key=True, autoincrement=False) ]

class Architecture(GenericTable):
    __tablename__ = "arches"

    __columns__ = [ Column("arch", String(8), primary_key=True) ]
