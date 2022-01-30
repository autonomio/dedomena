#!/usr/local/bin/python
# -*- coding: utf-8 -*-


def _get_results(keyword, url=None):
    
    import requests
    
    if url == None:    
        url = 'http://purl.bdrc.io/query/table/Etexts_chunks?L_NAME=('
        url += keyword
        url += ')&LG_NAME=bo&LI_NAME=1000&format=json'
    
    response = requests.get(url)

    return response.content


def bdrc(keyword):
    
    '''Get Etext chunks with meta-data from BDRC

    articles = bdrc('%E0%BD%A6%E0%BD%BA%E0%BD%98%E0%BD%A6%E0%BC%8B')
    
    keyword | str | The string to be searched for in the Etexts

    '''
    
    import json
    import pandas as pd
    
    out = []
    
    results_json = _get_results(keyword)
    results_dict = json.loads(results_json)
    
    no_of_pages = results_dict['numberOfPages']
    next_page_url = results_dict['pLinks']['nextGet']

    for page in range(no_of_pages - 1):

        results_json = _get_results(keyword, url=next_page_url)
        results_dict = json.loads(results_json)
        next_page_url = results_dict['pLinks']['nextGet']
        current_page_results = results_dict['results']['bindings']

        for i in range(len(current_page_results)):

            text = current_page_results[i]['lit']['value']
            text = text.replace('\n','').replace('↤', '').replace('↦', '').replace('#', '')
            score = current_page_results[i]['score']['value']
            uri = current_page_results[i]['txt']['value']

            out.append([text, score, uri])
        
    out = pd.DataFrame(out)
    out.columns = ['text', 'score', 'uri']
        
    return out
