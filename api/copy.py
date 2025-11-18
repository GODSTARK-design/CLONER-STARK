# api/copy.py
import asyncio
import json
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Set
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

app = FastAPI()

# ---------------- CONFIG ----------------
USER_AGENT = "Stark-Website-Cloner/1.0 (+https://stark.example)"
CONCURRENT_DOWNLOADS = 8
HTTP_TIMEOUT = 30  # seconds
TMP_ROOT = Path("/tmp")  # Vercel uses /tmp for ephemeral storage
# ----------------------------------------

# utilities
def ensure_scheme(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return "http://" + url
    return url

def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path.endswith("/"):
        path = path + "index.html"
    filename = path.lstrip("/")
    if not filename:
        filename = "index.html"
    # include hostname as prefix to avoid collisions across domains
    host = parsed.netloc.replace(":", "_")
    filename = os.path.join(host, filename)
    # remove query and fragments
    filename = re.sub(r"[\\:*?\"<>|]", "_", filename)
    if parsed.query:
        # append a short query fingerprint to avoid overwrites
        filename = filename + "_" + re.sub(r'[^0-9a-zA-Z]', '_', parsed.query)[:40]
    return filename

def make_workspace() -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    root = TMP_ROOT / f"stark_clone_{ts}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return root

def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

async def fetch_text(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text

async def fetch_bytes(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.content

def find_asset_urls(base_url: str, html: str) -> Set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    tags_attrs = [
        ("img", "src"),
        ("script", "src"),
        ("link", "href"),
        ("source", "src"),
        ("video", "poster"),
        ("audio", "src"),
    ]
    for tag, attr in tags_attrs:
        for el in soup.find_all(tag):
            v = el.get(attr)
            if v:
                urls.add(urljoin(base_url, v))

    # srcset
    for el in soup.find_all(srcset=True):
        ss = el.get("srcset")
        for part in ss.split(","):
            src = part.strip().split(" ")[0]
            if src:
                urls.add(urljoin(base_url, src))

    # inline style url(...)
    for el in soup.find_all(style=True):
        style = el.get("style")
        for m in re.findall(r"url\(([^)]+)\)", style):
            mclean = m.strip(' \'"')
            urls.add(urljoin(base_url, mclean))

    # look for CSS-imported URLs inside <style> tags
    for el in soup.find_all("style"):
        text = el.string or ""
        for m in re.findall(r"url\(([^)]+)\)", text):
            mclean = m.strip(' \'"')
            urls.add(urljoin(base_url, mclean))

    # also add hrefs (may include fonts/icons)
    for el in soup.find_all("a", href=True):
        # don't add regular page links as assets; we only want static resources
        href = el.get("href")
        if href and re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|woff2?|ttf|eot|mp4|webm|ico)", href, re.I):
            urls.add(urljoin(base_url, href))

    return urls

def extract_css_urls_from_text(base_url: str, css_text: str):
    urls = set()
    for m in re.findall(r"url\(([^)]+)\)", css_text):
        mclean = m.strip(' \'"')
        if mclean:
            urls.add(urljoin(base_url, mclean))
    # @import "..."
    for m in re.findall(r'@import\s+(?:url\()?["\']?([^"\')]+)', css_text):
        urls.add(urljoin(base_url, m))
    return urls

# SSE helper
async def sse_generator(web_url: str) -> AsyncGenerator[str, None]:
    web_url = ensure_scheme(web_url)
    root = make_workspace()
    start_time = time.time()
    zipped_name = f"stark_{root.name.split('_')[-1]}.zip"
    zipped_fullpath = TMP_ROOT / zipped_name

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers) as client:
        # fetch main html
        try:
            yield "event: status\ndata: fetching_html\n\n"
            resp = await client.get(web_url, timeout=HTTP_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
            yield "event: status\ndata: fetched_html\n\n"
        except Exception as e:
            yield f"event: error\ndata: failed_fetch_html: {str(e)}\n\n"
            return

        # collect asset urls
        asset_urls = set()
        asset_urls.update(find_asset_urls(web_url, html))

        soup = BeautifulSoup(html, "html.parser")
        css_links = []
        for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
            href = link.get("href")
            if href:
                css_links.append(urljoin(web_url, href))

        # fetch CSS to discover url(...) references
        css_texts = {}
        for css_url in css_links:
            try:
                yield f"event: status\ndata: fetching_css {css_url}\n\n"
                r = await client.get(css_url, timeout=HTTP_TIMEOUT, follow_redirects=True)
                if r.status_code == 200:
                    css_texts[css_url] = r.text
                    css_assets = extract_css_urls_from_text(web_url, r.text)
                    asset_urls.update(css_assets)
            except Exception:
                continue

        # include css links themselves
        asset_urls.update(css_links)

        # avoid adding the main page if present
        asset_urls.discard(web_url)

        total_items = len(asset_urls) + 1
        yield f"event: meta\ndata: {{\"total_items\": {total_items}}}\n\n"

        # save main html to workspace (will rewrite later)
        main_html_path = root / "index.html"
        main_html_path.parent.mkdir(parents=True, exist_ok=True)
        main_html_path.write_text(html, encoding="utf-8")

        # state trackers
        bytes_downloaded = 0
        bytes_since_last = 0
        last_update = time.time()
        downloaded_files = []

        sem = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

        async def download_and_save(url: str):
            nonlocal bytes_downloaded, bytes_since_last
            async with sem:
                try:
                    r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
                    r.raise_for_status()
                    content = r.content
                    local_rel = safe_filename_from_url(url)
                    local_path = root / local_rel
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(content)
                    bytes_downloaded += len(content)
                    bytes_since_last += len(content)
                    downloaded_files.append({"url": url, "local": str(local_path), "size": len(content)})
                    # emit asset event
                    ev = json.dumps({"url": url, "saved": str(local_path), "size": len(content)})
                    yield f"event: asset\ndata: {ev}\n\n"
                except Exception as e:
                    ev = json.dumps({"url": url, "error": str(e)})
                    yield f"event: asset_error\ndata: {ev}\n\n"

        # download assets in batches
        url_list = list(asset_urls)
        # create a queue of coroutines and run them in limited concurrency
        tasks = []
        for u in url_list:
            tasks.append(download_and_save(u))

        # run tasks while streaming their single yields
        async def run_tasks_and_emit(task_generators):
            nonlocal bytes_since_last, last_update
            # consume in chunks to avoid creating too many coroutines
            for i in range(0, len(task_generators), CONCURRENT_DOWNLOADS):
                batch = task_generators[i : i + CONCURRENT_DOWNLOADS]
                # wrap batch items to collect their yielded string
                async def consume(gen):
                    async for ev in gen:
                        return ev
                    return None
                results = await asyncio.gather(*[consume(g) for g in batch], return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        yield f"event: asset_error\ndata: {{\"error\":\"{str(res)}\"}}\n\n"
                    elif isinstance(res, str) and res:
                        yield res
                # emit progress
                now = time.time()
                elapsed = now - last_update
                if elapsed >= 0.5:
                    speed = int(bytes_since_last / max(elapsed, 1e-6))
                    bytes_since_last = 0
                    last_update = now
                    yield f"event: progress\ndata: {{\"bytes_total\":{bytes_downloaded}, \"speed_bps\":{speed}}}\n\n"

        try:
            async for ev in run_tasks_and_emit(tasks):
                yield ev
        except Exception as e:
            yield f"event: error\ndata: download_batch_failed: {str(e)}\n\n"
            # continue to try to rewrite whatever we have

        # rewrite HTML to point to local files
        try:
            yield "event: status\ndata: rewriting_html\n\n"
            html_text = main_html_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(html_text, "html.parser")

            def rewrite_attr(el, attr):
                val = el.get(attr)
                if not val:
                    return
                abs_url = urljoin(web_url, val)
                new_rel = safe_filename_from_url(abs_url)
                el[attr] = new_rel

            for img in soup.find_all("img"):
                rewrite_attr(img, "src")
            for scr in soup.find_all("script"):
                rewrite_attr(scr, "src")
            for link in soup.find_all("link"):
                rewrite_attr(link, "href")
            for src in soup.find_all("source"):
                rewrite_attr(src, "src")
            for vid in soup.find_all("video"):
                rewrite_attr(vid, "poster")

            # srcset
            for el in soup.find_all(srcset=True):
                ss = el.get("srcset")
                parts = []
                for part in ss.split(","):
                    src = part.strip().split(" ")[0]
                    abs_url = urljoin(web_url, src)
                    parts.append(safe_filename_from_url(abs_url))
                el["srcset"] = ", ".join(parts)

            # inline styles: replace url(...) with local filenames where possible
            for style_el in soup.find_all(style=True):
                style = style_el.get("style")
                def repl(match):
                    orig = match.group(1).strip(' \'"')
                    new = safe_filename_from_url(urljoin(web_url, orig))
                    return f"url('{new}')"
                new_style = re.sub(r"url\(([^)]+)\)", repl, style)
                style_el["style"] = new_style

            # write rewritten html
            main_html_path.write_text(soup.prettify(), encoding="utf-8")
            yield "event: status\ndata: html_rewritten\n\n"
        except Exception as e:
            yield f"event: error\ndata: rewrite_failed: {str(e)}\n\n"

        # create info.json
        try:
            yield "event: status\ndata: creating_info_json\n\n"
            info = {
                "cloned_from": web_url,
                "timestamp_utc": datetime.utcnow().isoformat() + "Z",
                "total_files_downloaded": len(downloaded_files) + 1,  # +1 for main html
                "downloaded_bytes": bytes_downloaded,
                "zip_name": zipped_name,
            }
            write_json(root / "info.json", info)
            yield "event: status\ndata: info_json_created\n\n"
        except Exception as e:
            yield f"event: error\ndata: info_json_failed: {str(e)}\n\n"

        # create zip
        try:
            yield "event: status\ndata: creating_zip\n\n"
            with zipfile.ZipFile(zipped_fullpath, "w", zipfile.ZIP_DEFLATED) as zf:
                for foldername, _, filenames in os.walk(root):
                    for filename in filenames:
                        file_path = os.path.join(foldername, filename)
                        arcname = os.path.relpath(file_path, root)
                        zf.write(file_path, arcname)
            yield "event: status\ndata: zip_created\n\n"
        except Exception as e:
            yield f"event: error\ndata: zip_failed: {str(e)}\n\n"
            return

        elapsed = time.time() - start_time
        final_meta = {
            "status": "done",
            "zip_path": str(zipped_fullpath),
            "zip_name": zipped_name,
            "elapsed_seconds": int(elapsed),
        }
        yield f"event: done\ndata: {json.dumps(final_meta)}\n\n"
        return

@app.get("/copy")
async def copy_endpoint(request: Request, web: str = Query(..., description="Target website URL")):
    """
    SSE streaming endpoint. Consume with EventSource:
    const es = new EventSource('/copy?web=https://example.com')
    """
    if not web:
        return {"error": "missing web parameter"}
    if not web.startswith("http://") and not web.startswith("https://"):
        web = "http://" + web

    async def event_stream():
        async for s in sse_generator(web):
            yield s

    return StreamingResponse(event_stream(), media_type="text/event-stream")
    
@app.get("/")
def home():
    return {
        "status": "API Running",
        "message": "Stark Website Cloner Online",
        "author": "STARK"
    }
    
@app.get("/download/{zipname}")
def download_zip(zipname: str):
    path = TMP_ROOT / zipname
    if not path.exists():
        return {"error": "not_found"}
    return FileResponse(path, filename=zipname, media_type="application/zip")
