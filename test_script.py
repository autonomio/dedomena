import dedomena as da

# test datasets.autonomio
datasets = da.datasets.autonomio()
for dataset in datasets:
    if dataset != 'news_articles':
        _null = da.datasets.autonomio(dataset)

    print('datasets.autonomio.' + dataset + ' : Tests passed')

# test datasets.pmlb
_null = da.datasets.pmlb()
datasets = da.datasets.pmlb('cancer')
for dataset in datasets:
    _null = da.datasets.pmlb(dataset)

    print('datasets.pmlb.' + dataset + ' : Tests passed')

# test apis.twitter
_null = da.apis.twitter('cars', 50)

print("apis.twitter : Tests passed")

# test apis.pubmed
_null = da.apis.pubmed('COVID', 50)

print("apis.pubmed : Tests passed")

# test apis.arxiv
_null = da.apis.arxiv('nlp', 50)

print("apis.arxiv : Tests passed")

# test apis.springer
_null = da.apis.springer('tibetan', 250)

print("apis.springer : Tests passed")

# test apis.bdrc
_null = da.apis.bdrc('%E0%BD%A6%E0%BD%BA%E0%BD%98%E0%BD%A6%E0%BC%8B')

print("apis.bdrc : Tests passed")
