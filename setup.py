#!/usr/bin/env python

from setuptools import setup


setup(
    name = "noby",
    version = "0.1",
    author = "Arti Zirk",
    author_email = "arti.zirk@gmail.com",
    description = "Minimal dockerfile builder",
    url = "https://github.com/artizirk/noby",
    py_modules = ["noby"],
    entry_points={'console_scripts': ['noby = noby:main']}
)
