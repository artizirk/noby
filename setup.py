#!/usr/bin/env python

from setuptools import setup
from noby import __version__


setup(
    name = "noby",
    version = __version__,
    author = "Arti Zirk",
    author_email = "arti.zirk@gmail.com",
    description = "Minimal dockerfile builder",
    url = "https://github.com/artizirk/noby",
    py_modules = ["noby"],
    entry_points={'console_scripts': ['noby = noby:main']}
)
