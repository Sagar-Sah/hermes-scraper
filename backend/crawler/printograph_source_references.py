"""Bounded, read-only Printograph references; no HTML, scripts or account data.

URL rules follow the independently tested private-page projection, with raw dot
segments rejected before URL joining as well. This module never fetches a URL.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urljoin, urlsplit

SOURCE_URLS = frozenset({
    "https://www.printograph.com/products/special-shapes/order",
    "https://www.printograph.com/products/catalogs/order",
})
MAX_URL_BYTES = 768
MAX_NAV_LINKS = 64
MAX_SCRIPT_REFERENCES = 256
SCRIPT_ROLES = {"orderForm.js": "order_form", "chunk-common.js": "common", "common.js": "common"}
NAV_HOSTS = frozenset({"www.printograph.com", "printograph.com"})
SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_\-.]{1,510}$", re.ASCII)
SAFE_VERSION = re.compile(r"^[0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)
BLOCKED_NAV = re.compile(r"(?:^|/)(?:account|login|logout|cart|checkout|service|api|admin|password|profile|oauth|auth|session)(?:/|$)", re.I)
FAMILIES = {
    "acrylic": re.compile(r"(?:^|[-_/])acrylic(?:[-_/\.]|$)", re.I),
    "cardboards": re.compile(r"(?:^|[-_/])cardboards?(?:[-_/\.]|$)", re.I),
    "corrugated": re.compile(r"(?:^|[-_/])corrugated(?:[-_/\.]|$)", re.I),
    "staggered": re.compile(r"(?:^|[-_/])staggered(?:[-_/\.]|$)", re.I),
    "folders": re.compile(r"(?:^|[-_/])folders?(?:[-_/\.]|$)", re.I),
    "catalogs": re.compile(r"(?:^|[-_/])(?:catalogs?|booklets?)(?:[-_/\.]|$)", re.I),
}


def _split_safe(raw, *, navigation=False):
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_URL_BYTES:
        raise ValueError("source_reference_invalid_url")
    if any(ord(c) < 33 or ord(c) > 126 for c in raw) or "\\" in raw:
        raise ValueError("source_reference_invalid_url")
    if not navigation and raw.startswith("//"):
        if not raw.startswith("//static.printograph.com/"):
            raise ValueError("source_reference_invalid_url")
        raw = "https:" + raw
        if len(raw.encode("utf-8")) > MAX_URL_BYTES:
            raise ValueError("source_reference_invalid_url")
    if navigation and (raw.startswith("//") or raw.startswith("#")):
        raise ValueError("source_reference_invalid_url")
    # urljoin normalizes relative dot segments; reject them before joining.
    raw_path = raw.split("?", 1)[0].split("#", 1)[0]
    if any(p in {".", ".."} for p in raw_path.split("/")):
        raise ValueError("source_reference_invalid_url")
    candidate = urljoin(next(iter(sorted(SOURCE_URLS))), raw) if navigation else raw
    try:
        parts = urlsplit(candidate)
        hosts = NAV_HOSTS if navigation else {"static.printograph.com"}
        if (parts.scheme != "https" or parts.hostname not in hosts
                or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.netloc != parts.hostname):
            raise ValueError("source_reference_invalid_url")
    except ValueError:
        raise ValueError("source_reference_invalid_url") from None
    if (parts.fragment or not SAFE_PATH.fullmatch(parts.path) or "%" in parts.path
            or any(p in {".", ".."} for p in parts.path.split("/"))):
        raise ValueError("source_reference_invalid_url")
    return candidate, parts


def script_url(raw):
    candidate, parts = _split_safe(raw)
    role = SCRIPT_ROLES.get(PurePosixPath(parts.path).name)
    if role is None:
        raise ValueError("source_reference_invalid_script")
    if parts.query:
        if "%" in parts.query or len(parts.query) > 80:
            raise ValueError("source_reference_invalid_script_query")
        try:
            items = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        except ValueError:
            raise ValueError("source_reference_invalid_script_query") from None
        if (len(items) != 1 or items[0][0] not in {"v", "ver", "version"}
                or not SAFE_VERSION.fullmatch(items[0][1])):
            raise ValueError("source_reference_invalid_script_query")
    return role, candidate


def navigation_url(raw):
    candidate, parts = _split_safe(raw, navigation=True)
    families = sorted(k for k, pattern in FAMILIES.items() if pattern.search(parts.path))
    if not parts.path.startswith("/products/") or parts.query or BLOCKED_NAV.search(parts.path) or not families:
        raise ValueError("source_reference_invalid_navigation")
    return candidate, families


def project_references(raw):
    """Revalidate the bounded browser projection; no raw input is reflected."""
    if not isinstance(raw, dict) or raw.get("source_url_match") is not True:
        raise ValueError("diagnosis_source_url_mismatch")
    scripts, navigation = raw.get("scripts"), raw.get("navigation")
    if (not isinstance(scripts, list) or len(scripts) > MAX_SCRIPT_REFERENCES
            or not isinstance(navigation, list) or len(navigation) > MAX_NAV_LINKS
            or raw.get("scripts_truncated") is not False):
        raise ValueError("source_reference_limits_exceeded")
    by_role = {}
    for value in scripts:
        role, url = script_url(value)
        if role in by_role and by_role[role] != url:
            raise ValueError("source_script_version_ambiguous")
        by_role[role] = url
    by_url = {}
    for value in navigation:
        url, families = navigation_url(value)
        by_url[url] = families
    return {
        "scripts": [{"role": role, "url": url} for role, url in sorted(by_role.items())],
        "script_roles_missing": sorted({"order_form", "common"} - by_role.keys()),
        "navigation": [{"url": url, "families": families} for url, families in sorted(by_url.items())],
        "navigation_truncated": raw.get("navigation_truncated") is True,
    }


SOURCE_REFERENCES_JS = r"""({source_url}) => {
  if (location.href !== source_url) return {source_url_match:false};
  const maxURL=768, scripts=[], navigation=[], uniqueNav=new Set();
  const printable=s => typeof s==='string' && s.length>0 && s.length<=maxURL &&
    /^[\x21-\x7e]+$/.test(s) && !s.includes('\\');
  const pathOK=p => /^\/[A-Za-z0-9/_\-.]{1,510}$/.test(p) &&
    !p.split('/').some(v=>v==='.'||v==='..');
  const parse=(raw,nav) => {
    if (!printable(raw)||raw.startsWith('#')) return null;
    if(raw.startsWith('//')) {
      if(nav||!raw.startsWith('//static.printograph.com/'))return null;
      raw='https:'+raw;
      if(!printable(raw))return null;
    }
    if (raw.split(/[?#]/)[0].split('/').some(v=>v==='.'||v==='..')) return null;
    try {
      const u=new URL(raw,nav?source_url:undefined);
      const authority=raw.startsWith('https://')?raw.slice(8).split('/')[0].split('?')[0].split('#')[0]:null;
      if(u.protocol!=='https:'||u.username||u.password||u.port||u.hash||!pathOK(u.pathname))return null;
      if(authority!==null&&authority!==u.hostname)return null;
      if(nav) {
        if(!['www.printograph.com','printograph.com'].includes(u.hostname)||u.search||
          !u.pathname.startsWith('/products/')||
          /(?:^|\/)(?:account|login|logout|cart|checkout|service|api|admin|password|profile|oauth|auth|session)(?:\/|$)/i.test(u.pathname)||
          !/(?:^|[-_/])(?:acrylic|cardboards?|corrugated|staggered|folders?|catalogs?|booklets?)(?:[-_/\.]|$)/i.test(u.pathname))return null;
      } else {
        if(!raw.startsWith('https://')||u.hostname!=='static.printograph.com'||
          !['orderForm.js','chunk-common.js','common.js'].includes(u.pathname.split('/').pop())||
          (u.search&&!/^\?(?:v|ver|version)=[0-9][A-Za-z0-9._-]{0,63}$/.test(u.search)))return null;
      }
      return u.href;
    } catch {return null;}
  };
  const scriptNodes=document.querySelectorAll('script[src]');
  for(const e of [...scriptNodes].slice(0,256)) {const u=parse(e.getAttribute('src'),false);if(u)scripts.push(u);}
  const anchors=document.querySelectorAll('a[href]');let navTruncated=anchors.length>2048;
  for(const e of [...anchors].slice(0,2048)) {
    const u=parse(e.getAttribute('href'),true);if(!u||uniqueNav.has(u))continue;
    if(navigation.length>=64){navTruncated=true;continue;}
    uniqueNav.add(u);navigation.push(u);
  }
  return {source_url_match:true,scripts,navigation,scripts_truncated:scriptNodes.length>256,
    navigation_truncated:navTruncated};
}"""
