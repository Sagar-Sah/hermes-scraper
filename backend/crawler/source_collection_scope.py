"""Closed, run-local selection for an existing durable source collection."""


def resume_scope(job, urls=None, *, max_nodes_per_product=None, node_limit_max=None):
    if urls is None:
        if max_nodes_per_product is not None:
            raise ValueError('A node-budget increase requires selected page URLs')
        return None
    if type(urls) not in (list, tuple) or not 1 <= len(urls) <= 1000:
        raise ValueError('Select 1–1000 existing source page URLs')
    if any(type(url) is not str or not url or len(url) > 4096 for url in urls):
        raise ValueError('Select exact existing source page URLs')
    if len(set(urls)) != len(urls):
        raise ValueError('Duplicate source page selectors are not allowed')
    pages = job['pages']
    selected = set(urls)
    matches = [page for page in pages if page['url'] in selected]
    if len(matches) != len(selected) or {page['url'] for page in matches} != selected:
        raise ValueError('Selected URLs must belong uniquely to this source collection')
    if max_nodes_per_product is not None:
        previous = job.get('max_nodes_per_product')
        if node_limit_max is None:
            raise ValueError('This source profile does not use a page-node budget')
        if (type(max_nodes_per_product) is not int or type(previous) is not int
                or not previous < max_nodes_per_product <= node_limit_max
                or any(type(page.get('nodes')) is not int
                       or page['nodes'] >= max_nodes_per_product for page in matches)):
            raise ValueError('The requested node limit must increase the existing budget and exceed selected node counts')
    history = job.get('resume_history', [])
    if type(history) is not list or len(history) >= 256:
        raise ValueError('Source resume history needs review before another selected run')
    # Preserve durable job order, detach caller-owned lists, never normalize URLs.
    return tuple(page['url'] for page in matches)


def scoped_pages(job, urls):
    return [page for page in job['pages'] if urls is None or page['url'] in urls]


def record_resume_scope(job, urls, node_limit, requested_at):
    if urls is not None:
        job.setdefault('resume_history', []).append({
            'requested_at': requested_at,
            'urls': list(urls),
            'requested_max_nodes_per_product': node_limit,
            'original_job_max_nodes_per_product': job.get('max_nodes_per_product'),
            'scope': 'selected_pages_only',
        })
