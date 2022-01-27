def autonomio(dataset=None):

    '''Load a dataset

    dataset : str
        One of the available datasets:
            'bitcoin_price'
            'breast_cancer'
            'cervical_cancer'
            'diabetes'
            'election_in_twitter'
            'fake_news'
            'icu_mortality'
            'icu_patients'
            'iris'
            'news_articles'
            'parties_and_employment'
            'personality_types_and_cannabis_use'
            'programmatic_ad_fraud'
            'titanic'
            'icu_burden'
            

    Dataset source: https://github.com/autonomio/datasets/tree/master/autonomio-datasets

    '''

    import pandas as pd

    datasets = ['bitcoin_price',
                'breast_cancer',
                'cervical_cancer',
                'diabetes',
                'election_in_twitter',
                'fake_news',
                'icu_mortality',
                'icu_patients',
                'iris',
                'news_articles',
                'parties_and_employment',
                'personality_types_and_cannabis_use',
                'programmatic_ad_fraud',
                'titanic'
                'icu_burden']

    if dataset is None:
        return datasets

    url_1 = 'https://raw.githubusercontent.com/'
    url_2 = 'autonomio/datasets/master/autonomio-datasets/'
    url_end = '.csv'
    url = url_1 + url_2 + dataset + url_end

    return pd.read_csv(url)
