#!/usr/bin/env python

from setuptools import setup
from noby import __version__


setup(
    version = __version__,
    entry_points = {'console_scripts': ['noby = noby:main']}
)
