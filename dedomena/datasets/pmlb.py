def pmlb(what_to_do=None):

    '''Penn Machine Learning Benchmarks

    what_to_do : None or str
        None returns a list with available dataset names,
        and a string input will try to load a dataset from
        pmlb, and fallback to keyword search instead.

    '''

    import re
    from pmlb import fetch_data, dataset_names

    # show the available datasets
    if what_to_do is None:
        return dataset_names
    try:
        # try getting the data with string
        return fetch_data(what_to_do)
    except ValueError:
        # name is not in list, assume as search
        out = []
        for i in dataset_names:
            if len(re.findall(what_to_do, i.lower())) > 0:
                out.append(i)
        return out
