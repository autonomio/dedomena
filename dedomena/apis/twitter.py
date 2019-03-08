def twitter(search_string, n):

    """Search Twitter API for keywords"""

    import twintel as tw

    return tw.search(search_string, n)
