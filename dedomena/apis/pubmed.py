def pubmed(title_keywords, n=500, docs=False):
    
    '''Get articles with meta-data from PubMed

    pubs = pubmed_query('nutrition')
    
    title_keywords : str
        The string to be searched for in the title of the
        articles.
    n : int
        Number of articles to return.
    docs : bool
        Instead of dataframe with multiple columns,
        just return abstracts as a list of lists.

    '''

    from pymed import PubMed
    import json
    import pandas as pd

    out = []

    pubmed = PubMed(tool="literview", email="admin@autonom.io")
    query = title_keywords + "[Title]"
    results = pubmed.query(query, max_results=n)

    for article in results:
        out.append(article.toJSON())

    out2 = []

    for i in range(len(out)):

        j = json.loads(out[i])

        try:
            journal = j['journal']
        except:
            journal = ''

        try:
            keywords = j['keywords']
        except:
            keywords = []

        out2.append([j['title'],
                     journal,
                     j['publication_date'],
                     keywords,
                     j['abstract']])

    out = pd.DataFrame(out2)
    out.columns = ['title',
                   'journal',
                   'publication_date',
                   'keywords',
                   'abstract']
        
    if docs:
        out = [[doc] for doc in out.abstract.values]

    return out