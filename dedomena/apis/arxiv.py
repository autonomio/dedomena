def arxiv(keyword, n):
    
    '''Get articles with meta-data from ArXiv

    articles = arxiv('tibetan', 50)
    
    keyword | str | The string to be searched for in the title of the articles.
    n | int | Number of articles to return.

    '''

    import urllib
    import xmltodict
    import pandas as pd
    
    url = 'http://export.arxiv.org/api/query?search_query=all:'
    query = keyword + '&start=0&max_results=' + str(n)
    data = urllib.request.urlopen(url + query)
    results_xml = data.read().decode('utf-8')

    out = []

    results_dict = xmltodict.parse(results_xml)

    results_list = results_dict['feed']['entry']

    for i in range(len(results_list)):

        title = results_list[i]['title']
        publication_date = results_list[i]['published']
        abstract = results_list[i]['summary']

        out.append([title, publication_date, abstract])

    out = pd.DataFrame(out)
    out.columns = ['title', 'publication_date', 'abstract']
        
    return out