"""Rebuild the dynamic sections of README.md.

Each section sits between marker comments, e.g.
    <!-- writing starts --> ... <!-- writing ends -->
If a source can't be fetched, that section is left exactly as it was,
so a flaky network never wipes the profile.
"""

import os
import re
import sys
import pathlib
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

USER = "dhriti-kumawat"
SITE = "https://dhritikumawat.vercel.app"
LIMIT = 5
ROOT = pathlib.Path(__file__).parent
TIMEOUT = 20

# Private repos to show in "Recent work". They link to the live product.
PRIVATE_PROJECTS = [
    {"repo": "haulbook", "title": "Haulbook (private)",
     "url": "https://haulbook.vercel.app/"},
]

session = requests.Session()
session.headers["User-Agent"] = f"{USER}-profile-readme"
token = os.environ.get("GITHUB_TOKEN")
gh_headers = {"Accept": "application/vnd.github+json"}
if token:
    gh_headers["Authorization"] = f"Bearer {token}"


def replace_chunk(content, marker, chunk):
    pattern = re.compile(
        rf"<!-- {marker} starts -->.*?<!-- {marker} ends -->", re.DOTALL
    )
    new = f"<!-- {marker} starts -->\n{chunk}\n<!-- {marker} ends -->"
    return pattern.sub(lambda _: new, content)


def bullet(title, url, date=None):
    title = title.replace("[", "(").replace("]", ")").strip()
    return f"* [{title}]({url})" + (f" - {date}" if date else "")


# ---------- Recent work: releases first, then recently pushed repos ----------

def fetch_recent_work():
    r = session.get(
        f"https://api.github.com/users/{USER}/repos",
        params={"sort": "pushed", "per_page": 30, "type": "owner"},
        headers=gh_headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    repos = [
        repo for repo in r.json()
        if not repo["fork"] and not repo["archived"] and repo["name"] != USER
    ]

    items = []  # (date, line)
    for repo in repos[:10]:
        rel = session.get(
            f"https://api.github.com/repos/{USER}/{repo['name']}/releases",
            params={"per_page": 3},
            headers=gh_headers,
            timeout=TIMEOUT,
        )
        if rel.ok:
            for release in rel.json():
                if release.get("draft"):
                    continue
                date = (release.get("published_at") or "")[:10]
                items.append((date, bullet(
                    f"{repo['name']} {release['tag_name']}",
                    release["html_url"], date)))

    seen = {line.split("]")[0] for _, line in items}
    for repo in repos:
        date = repo["pushed_at"][:10]
        line = bullet(repo["name"], repo["html_url"], date)
        if not any(s.startswith(f"* [{repo['name']} ") for s in seen):
            items.append((date, line))

    # Private projects: link to the live site, never to the private repo.
    for project in PRIVATE_PROJECTS:
        date = private_push_date(project["repo"])
        items.append((date or "", bullet(project["title"], project["url"], date)))

    items.sort(key=lambda x: x[0], reverse=True)
    return [line for _, line in items[:LIMIT]]


def private_push_date(repo):
    """Last push date of a private repo, if PROFILE_TOKEN can read it.

    Only the date is used; nothing else about the repo is published.
    """
    private_token = os.environ.get("PROFILE_TOKEN")
    if not private_token:
        return None
    try:
        r = session.get(
            f"https://api.github.com/repos/{USER}/{repo}",
            headers={**gh_headers, "Authorization": f"Bearer {private_token}"},
            timeout=TIMEOUT,
        )
        return r.json()["pushed_at"][:10] if r.ok else None
    except (requests.RequestException, KeyError, ValueError):
        return None


# ---------- Writing and experiments from the portfolio site ----------

def parse_date(text):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def from_feed(url):
    r = session.get(url, timeout=TIMEOUT)
    if not r.ok or b"<rss" not in r.content[:500] and b"<feed" not in r.content[:500]:
        return None
    root = ET.fromstring(r.content)
    posts = []
    for item in root.iter("item"):
        posts.append((item.findtext("title"), item.findtext("link"),
                      parse_date(item.findtext("pubDate") or "")))
    return posts or None


def from_page(path):
    """Read post links like /writing/<slug>/ from a listing page."""
    url = f"{SITE}/{path}/"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if not re.search(rf"/{path}/[^/]+/?$", href) or href in seen:
            continue
        heading = a.find(["h2", "h3", "h4"])
        if heading is None and a.find_parent(["h2", "h3", "h4"]):
            heading = a.find_parent(["h2", "h3", "h4"])
        title = (heading or a).get_text(" ", strip=True)
        if not title or title.lower().startswith("read"):
            continue
        m = re.search(r"\b(\d{1,2} [A-Z][a-z]{2,8} \d{4})\b", a.get_text(" "))
        seen.add(href)
        posts.append((title, href, parse_date(m.group(1)) if m else None))
    return posts


def fetch_writing():
    for feed in (f"{SITE}/rss.xml", f"{SITE}/writing/rss.xml"):
        try:
            posts = from_feed(feed)
            if posts:
                break
        except (requests.RequestException, ET.ParseError):
            posts = None
    if not posts:
        posts = from_page("writing")
    return [bullet(*p) for p in posts[:LIMIT]]


def fetch_experiments():
    return [bullet(t, u) for t, u, _ in from_page("experiments")[:LIMIT]]


def main():
    readme = ROOT / "README.md"
    content = readme.read_text()
    for marker, fetch in (("recent_work", fetch_recent_work),
                          ("writing", fetch_writing),
                          ("experiments", fetch_experiments)):
        try:
            lines = fetch()
        except Exception as e:  # keep the old section on any failure
            print(f"{marker}: skipped ({e})", file=sys.stderr)
            continue
        if lines:
            content = replace_chunk(content, marker, "\n".join(lines))
            print(f"{marker}: {len(lines)} items")
        else:
            print(f"{marker}: nothing found, left unchanged", file=sys.stderr)
    readme.write_text(content)


if __name__ == "__main__":
    main()
