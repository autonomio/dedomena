def _get_results(keyword, n, start_page=1):
    
    import json
    import urllib
    
    url = 'http://api.springernature.com/meta/v2/json?q=title:'
    keyword = 'tibetan'+ '%20sort:date&s=' + str(start_page) + '&p=' + str(n) 
    api_key = '&api_key=0e531c4b40c883c575cc5df58dc8beb4'
    
    data = urllib.request.urlopen(url + keyword + api_key)
    results_json = data.read().decode('utf-8')
    results_dict = json.loads(results_json)
    
    return results_dict


def _extract_article_meta(results_dict, out):

    for i in range(len(results_dict)):

        try:
            title = results_dict[i]['title']
        except KeyError:
            title = None
        
        try:
            journal = results_dict[i]['publicationName']
        except KeyError:
            journal = None
        
        try:
            publication_date = results_dict[i]['publicationDate']
        except KeyError:
            publication_date = None
        
        try:
            keywords = results_dict[i]['keyword']
        except KeyError:
            keywords = None
        
        try:
            abstract = results_dict[i]['abstract']
        except KeyError:
            abstract = None

        out.append([title, journal, publication_date, keywords, abstract])
        
    return out


def springer(keywords, n):
    
    '''Get articles with meta-data from Springer

    articles = springer('tibetan', 400)
    
    keyword | str | The string to be searched for in the title of the articles.
    n | int | Number of articles to return.

    '''

    import math
    import pandas as pd

    out = []

    # get the initial results
    results_dict = _get_results(keywords, n)

    # get number of pages
    total_articles = int(results_dict['result'][0]['total'])
    
    if total_articles > n:
        total_articles = n

    number_of_pages = math.ceil(total_articles / 100)
    
    # write the first results
    out = _extract_article_meta(results_dict['records'], out)

    # repeat for the following pages
    for i in range(number_of_pages + 1)[2:]:
        results_dict = _get_results(keyword=keywords, n=n, start_page=i)
        out = _extract_article_meta(results_dict['records'], out)
        
    out = pd.DataFrame(out)
    out.columns = ['title', 'journal', 'publication_date', 'keywords', 'abstract']
    
    return out
