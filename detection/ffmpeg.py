#! /usr/bin/env python
# -*- coding: UTF-8 -*-
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General License for more details.
#
# You should have received a copy of the GNU General License
# along with self program.  If not, see <http://www.gnu.org/licenses/>
#

# The ptrace part is largely based on:
# https://github.com/haypo/python-ptrace/blob/11a117427faee52ebb54de0bc6fe21738cbff7a4/strace.py

from __future__ import absolute_import, print_function

import os
import subprocess
import tempfile


def detect(f):
    from detection import filetype

    f = os.path.abspath(f)
    major, minor = filetype(f).split('/')
    with tempfile.NamedTemporaryFile() as tmp:
        args = ['ffmpeg',
                '-loglevel', 'warning',
                '-y',
                '-i', f,
                '-c', 'copy',
                '-f', minor,
                tmp.name]
        subprocess.call(args)
        return os.path.getsize(tmp.name), False
