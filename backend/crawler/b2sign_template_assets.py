"""Bounded, source-bound B2Sign artwork-template downloads.

Only links already observed in a saved B2Sign diagnosis are eligible. Files
are fetched in the signed-in headed browser context, verified and cached.
"""
import asyncio
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


ORIGIN = 'https://www.b2sign.com'
MAX_PDF = 20 * 1024 * 1024


def approved_link(url):
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.hostname == 'www.b2sign.com'
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment
            and re.fullmatch(r'/downloadable/[a-f0-9]{40}', parts.path) is not None)


async def collect(diagnosis, pool, store):
    source = diagnosis.get('url', '')
    parts = urlsplit(source)
    if parts.scheme != 'https' or parts.hostname != 'www.b2sign.com' or parts.query or parts.fragment:
        raise ValueError('b2sign_source_required')
    templates = diagnosis.get('content', {}).get('artwork_templates', [])
    if not isinstance(templates, list) or len(templates) > 16 or not templates:
        raise ValueError('observed_template_links_required')
    urls = [item.get('url') for item in templates]
    if not all(approved_link(url) for url in urls) or len(urls) != len(set(urls)):
        raise ValueError('template_links_outside_diagnosis_scope')
    folder = Path(store.root) / 'b2sign-template-assets'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    results = []
    async with pool.page(ORIGIN, headless=False) as page:
        response = await page.goto(source, wait_until='domcontentloaded', timeout=30000)
        if not response or response.status != 200:
            raise ValueError('b2sign_source_unavailable')
        title = (await page.locator('h1').first.inner_text(timeout=10000)).strip()
        if title != diagnosis.get('title'):
            raise ValueError('b2sign_source_identity_changed')
        rendered = set(await page.locator('a[href]').evaluate_all(
            '(links) => links.map(link => link.href).filter(url => url.startsWith("https://www.b2sign.com/downloadable/"))'))
        if not set(urls).issubset(rendered):
            raise ValueError('diagnosed_template_link_missing_from_source')
        for index, item in enumerate(templates):
            url = item['url']
            key = hashlib.sha256(url.encode()).hexdigest()
            path = folder / (key + '.pdf')
            if path.is_file():
                body = path.read_bytes()
                if len(body) <= MAX_PDF and body.startswith(b'%PDF-'):
                    results.append({'url': url, 'choice_hint': item.get('choice_hint', ''),
                                    'status': 'verified_pdf', 'file': str(path),
                                    'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                                    'cached': True})
                    continue
                raise ValueError('template_cache_invalid')
            if index:
                await asyncio.sleep(1)
            try:
                asset = await page.context.request.get(url, headers={'Accept': 'application/pdf'},
                                                       max_redirects=0, timeout=30000)
                size = asset.headers.get('content-length', '')
                if (asset.status != 200 or size and (not size.isdigit() or int(size) > MAX_PDF)):
                    results.append({'url': url, 'choice_hint': item.get('choice_hint', ''),
                                    'status': 'unavailable', 'reason': 'source_response_unverified',
                                    'http_status': asset.status})
                    continue
                body = await asset.body()
                if not body.startswith(b'%PDF-') or len(body) > MAX_PDF:
                    results.append({'url': url, 'choice_hint': item.get('choice_hint', ''),
                                    'status': 'unavailable', 'reason': 'pdf_validation_failed'})
                    continue
                tmp = folder / (key + '.tmp')
                tmp.write_bytes(body)
                tmp.chmod(0o600)
                os.replace(tmp, path)
                results.append({'url': url, 'choice_hint': item.get('choice_hint', ''),
                                'status': 'verified_pdf', 'file': str(path),
                                'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                                'cached': False})
            except Exception as exc:
                results.append({'url': url, 'choice_hint': item.get('choice_hint', ''),
                                'status': 'unavailable', 'reason': type(exc).__name__})
    return {'diagnosis_id': diagnosis['id'], 'source_url': source,
            'source_data_finalized': False, 'templates': results}
