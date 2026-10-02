#! /usr/bin/env python
#
# Copyright (C) 2019 Mikko Kotila

DESCRIPTION = "Reproducible research data sources for agents and scientists"
LONG_DESCRIPTION = """\
Dedomena provides compact, high-throughput access to OpenAlex, Europe PMC
and EPO patent data, with consistent provenance, durable caching and replay.
Legacy dataset and API functions remain available through the legacy extra.
"""

DISTNAME = 'Dedomena'
MAINTAINER = 'Mikko Kotila'
MAINTAINER_EMAIL = 'mailme@mikkokotila.com'
URL = 'http://autonom.io'
LICENSE = 'MIT'
DOWNLOAD_URL = 'https://github.com/autonomio/dedomena/'
VERSION = '0.4.0'

try:
    from setuptools import setup
    _has_setuptools = True
except ImportError:
    from distutils.core import setup

install_requires = ['httpx>=0.27,<1', 'defusedxml>=0.7,<1']
legacy_requires = ['pandas', 'pymed', 'twintel', 'pmlb', 'xmltodict']

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
          install_requires=install_requires,
          extras_require={"legacy": legacy_requires},
          python_requires=">=3.10",
          packages=['dedomena',
                    'dedomena.apis',
                    'dedomena.datasets',
                    'dedomena.generators',
                    'dedomena.sources'],

          classifiers=[
                'Intended Audience :: Science/Research',
                'Programming Language :: Python :: 3',
                'License :: OSI Approved :: MIT License',
                'Topic :: Scientific/Engineering :: Human Machine Interfaces',
                'Topic :: Scientific/Engineering :: Artificial Intelligence',
                'Topic :: Scientific/Engineering :: Mathematics',
                'Operating System :: POSIX',
                'Operating System :: Unix',
                'Operating System :: MacOS'])
