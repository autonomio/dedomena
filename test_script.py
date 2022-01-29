import dedomena as da

# test datasets.autonomio
datasets = da.datasets.autonomio()
for i in datasets:
    if i != 'news_articles':
        _null = da.datasets.autonomio(i)

print("datasets.autonomio : Tests passed")

# test datasets.pmlb
_null = da.datasets.pmlb()
datasets = da.datasets.pmlb('cancer')
for dataset in datasets:
    _null = da.datasets.pmlb(dataset)

print("datasets.pmlb : Tests passed")

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
