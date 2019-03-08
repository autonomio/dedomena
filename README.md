<h1 align="center">
  <br>
  <a href="http://autonom.io"><img src="https://raw.githubusercontent.com/autonomio/dedomena/master/logo.png" alt="Dedomena"></a>
  <br>
</h1>

<h3 align="center">A high-level data API for deep learning and data science.</h3>

<p align="center">

  <a href="https://travis-ci.org/autonomio/dedomena">
    <img src="https://img.shields.io/travis/autonomio/dedomena/master.svg?style=for-the-badge&logo=appveyor" alt="Signs Travis">
  </a>

  <a href="https://coveralls.io/github/autonomio/dedomena">
    <img src="https://img.shields.io/coveralls/github/autonomio/dedomena.svg?style=for-the-badge&logo=appveyor" alt="Signs Coveralls">
  </a>

</p>

<p align="center">
  <a href="#Key-Features">Dedomena</a> •
  <a href="#Key-Features">Key Features</a> •
  <a href="#Examples">Examples</a> •
  <a href="#Install">Install</a> •
  <a href="#Support">Support</a> •
  <a href="https://autonomio.github.io/docs_dedomena">Docs</a> •
  <a href="https://github.com/autonomio/dedomena/issues">Issues</a> •
  <a href="#License">License</a> •
  <a href="https://github.com/autonomio/dedomena/archive/master.zip">Download</a>
</p>
<hr>
<p align="center">
A very high level API for accessing datasets, data APIs, and for generating synthetic and random datasets for deep learning and other research purposes.

### Dedomena

Dedomena is a very high level API for accessing datasets, data APIs, and for generating synthetic and random datasets for deep learning and other research purposes. Dedomena provides an easy-to-use and remember three level namespace to 'datasets', 'apis', and 'generators'.

### Key Features

Instant access to thousands of datasets through a single interface.

- Twitter API [info](https://github.com/mikkokotila/twint)
- Autonomio Datasets [info](https://github.com/autonomio/datasets)
- Pandas datareader [info](https://pandas-datareader.readthedocs.io/en/latest/#id1)
- PMLB [info](https://github.com/EpistasisLab/penn-ml-benchmarks)
- MIMIC-III^^ [info](https://mimic.physionet.org/)

^^Requires approval for access.

### Examples

    # get a specific dataset from a provider
    da.datasets.autonomio('icu_mortality')

    # see all the datasets under a provider
    da.datasets.autonomio()


### Install

Stable version:

#### `pip install dedomena`

Daily development version:

#### `pip install git+https://github.com/autonomio/dedomena`

### Support

If you want ask a **"how can I use Signs to..."** question, the right place is [StackOverflow](https://stackoverflow.com/questions/ask).

If you found a bug or want to suggest a feature, check the [issues](https://github.com/autonomio/dedomena/issues) or [create](https://github.com/autonomio/dedomena/issues/new/choose) a new issue.


### License

[MIT License](https://github.com/autonomio/dedomena/blob/master/LICENSE)
