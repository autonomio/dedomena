import dedomena as da

# test datasets.autonomio
datasets = da.datasets.autonomio()
for i in datasets:
    if i != 'news_articles':
        _null = da.datasets.autonomio(i)

# test datasets.pmlb
_null = da.datasets.pmlb()
datasets = da.datasets.pmlb('cancer')
for dataset in datasets:
    _null = da.datasets.pmlb(dataset)

# test apis.twitter
_null = da.apis.twitter('cars', 50)

# test apis.pubmed
_null = da.apis.pubmed('COVID', 50)

# test apis.arxiv
_null = da.apis.arxiv('nlp', 50)
