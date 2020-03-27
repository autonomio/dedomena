#! /usr/bin/env python
#
# Copyright (C) 2019 Mikko Kotila

DESCRIPTION = "A unified Data API for deep learning and data science"
LONG_DESCRIPTION = """\
Dedomena unifies many common and useful data APIs into an easy to access
namespace, together with facilities for creating synthetic datasets for
deep learning models and other data science purposes.
"""

DISTNAME = 'Dedomena'
MAINTAINER = 'Mikko Kotila'
MAINTAINER_EMAIL = 'mailme@mikkokotila.com'
URL = 'http://autonom.io'
LICENSE = 'MIT'
DOWNLOAD_URL = 'https://github.com/autonomio/dedomena/'
VERSION = '0.0.6'

try:
    from setuptools import setup
    _has_setuptools = True
except ImportError:
    from distutils.core import setup


if __name__ == "__main__":

    setup(name=DISTNAME,
          author=MAINTAINER,
          author_email=MAINTAINER_EMAIL,
          maintainer=MAINTAINER,
          maintainer_email=MAINTAINER_EMAIL,
          description=DESCRIPTION,
          long_description=LONG_DESCRIPTION,
          license=LICENSE,
          url=URL,
          version=VERSION,
          download_url=DOWNLOAD_URL,
          install_requires=['pmlb', 'pandas', 'twintel', 'pymed'],
          packages=['dedomena',
                    'dedomena.apis',
                    'dedomena.datasets',
                    'dedomena.generators'],

          classifiers=[
                'Intended Audience :: Science/Research',
                'Programming Language :: Python :: 3.6',
                'License :: OSI Approved :: MIT License',
                'Topic :: Scientific/Engineering :: Human Machine Interfaces',
                'Topic :: Scientific/Engineering :: Artificial Intelligence',
                'Topic :: Scientific/Engineering :: Mathematics',
                'Operating System :: POSIX',
                'Operating System :: Unix',
                'Operating System :: MacOS'])
