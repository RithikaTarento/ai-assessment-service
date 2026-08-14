import os
import json
import asyncio
import httpx
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from .config import SEARCH_API_URL, TRANSCODER_STATS_URL, API_HEADERS, GCS_COURSE_CONTENT_PREFIX
from .storage import get_storage_service

logger = logging.getLogger(__name__)

async def fetch_course_data(course_id: str, output_base_path: Path):
    """
    Main entry point to fetch course data, PDFs, and VTTs.
    Replicates the logic from pdf_video_meta.html.
    """
    output_base_path.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    storage = get_storage_service()
    gcs_prefix = f"{GCS_COURSE_CONTENT_PREFIX}/{course_id}"

    async with httpx.AsyncClient(timeout=30.0, headers=API_HEADERS) as client:
        # Tier 1: Local disk cache
        potential_cache = output_base_path / course_id / "metadata.json"
        if potential_cache.exists():
            logger.info(f"Local cache hit for {course_id}. Skipping download.")
            return True

        # Tier 2: GCS cache — populate local disk then proceed
        course_local_path = output_base_path / course_id
        found_in_gcs = await loop.run_in_executor(
            None, storage.sync_directory_from_storage, gcs_prefix, course_local_path
        )
        if found_in_gcs:
            logger.info(f"GCS cache hit for {course_id}. Restored to local disk.")
            return True

        # Tier 3: Fetch from Learning API
        # 1. Fetch Root Course
        root_node = await search_content(client, course_id)
        if not root_node:
            logger.error(f"Course {course_id} not found.")
            return False

        course_folder = output_base_path / (root_node.get("identifier") or course_id)
        course_folder.mkdir(exist_ok=True)

        # 2. Process Root Node (Metadata, PDFs, Videos)
        await process_node(client, root_node, course_folder)

        # 3. Process Leaf Nodes
        leaf_nodes = root_node.get("leafNodes", [])
        for leaf_id in leaf_nodes:
            try:
                leaf_node = await search_content(client, leaf_id)
                if leaf_node:
                    leaf_folder = course_folder / leaf_node.get("identifier")
                    leaf_folder.mkdir(exist_ok=True)
                    await process_node(client, leaf_node, leaf_folder)
            except Exception as e:
                logger.error(f"Error processing leaf node {leaf_id}: {e}")

    # Sync fetched content to GCS for cross-pod reuse
    try:
        await loop.run_in_executor(
            None, storage.sync_directory_to_storage, course_folder, gcs_prefix
        )
        logger.info(f"Course content for {course_id} synced to GCS at {gcs_prefix}")
    except Exception as e:
        logger.warning(f"Failed to sync course content to GCS for {course_id}: {e}")

    return True

async def search_content(client: httpx.AsyncClient, identifier: str) -> Optional[Dict[str, Any]]:
    body = {
        "request": {
            "filters": {"identifier": identifier},
            "isSecureSettingsDisabled": True,
            "status": ["Live"],
            "fields": [],
            "limit": 1
        }
    }
    try:
        resp = await client.post(SEARCH_API_URL, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("content", [None])[0]
    except Exception as e:
        logger.error(f"Search failed for {identifier}: {e}")
        return None

async def process_node(client: httpx.AsyncClient, node: Dict[str, Any], folder: Path):
    # Save Metadata
    metadata = extract_metadata(node)
    (folder / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # Find and Download PDFs
    pdfs = find_pdf_resources(node)
    pdf_links = []
    for pdf in pdfs:
        try:
            filename = sanitize_filename(pdf["name"]) + ".pdf"
            file_path = folder / filename
            await download_file(client, pdf["url"], file_path)
            pdf_links.append(f"{pdf['name']} - {pdf['url']}")
        except Exception as e:
            logger.error(f"Failed to download PDF {pdf['name']}: {e}")
            pdf_links.append(f"{pdf['name']} - [FAILED] {pdf['url']}")
    
    (folder / "pdf_links.txt").write_text("\n".join(pdf_links), encoding="utf-8")

    # Find and Download VTTs
    videos = find_video_mp4_children(node)
    english_vtt_content = ""
    
    for video in videos:
        video_id = video.get("identifier")
        video_name = video.get("name", video_id)
        video_folder = folder / sanitize_filename(video_name)
        video_folder.mkdir(exist_ok=True)

        vtt_text = await fetch_vtt_for_video(client, video_id, video_folder)
        if vtt_text:
            english_vtt_content += f"\n\nNOTE: From video \"{video_name}\"\n\n{vtt_text}\n"

    (folder / "english_subtitles.vtt").write_text(english_vtt_content.strip() or "// No English subtitles found", encoding="utf-8")

def extract_metadata(node: Dict[str, Any]) -> Dict[str, Any]:
    competencies = node.get("competencies_v6", [])
    comp_str = "\n".join([c.get("competencyAreaName", "") for c in competencies if c.get("competencyAreaName")])
    
    return {
        "identifier": node.get("identifier", ""),
        "name": node.get("name", "N/A"),
        "description": node.get("description", ""),
        "keywords": node.get("keywords", []),
        "organisation": node.get("organisation", ["N/A"])[0],
        "competencies_v6": comp_str,
        "instructions": strip_html(node.get("instructions", "")),
        "courseCategory": node.get("courseCategory"),
        "scorm": False
    }

def find_pdf_resources(node: Dict[str, Any], found=None) -> List[Dict[str, str]]:
    if found is None:
        found = []
    
    if node.get("mimeType") == "application/pdf" and node.get("artifactUrl"):
        found.append({"name": node.get("name", "Unnamed PDF"), "url": node.get("artifactUrl")})
    
    for child in node.get("children", []):
        find_pdf_resources(child, found)
    return found

def find_video_mp4_children(node: Dict[str, Any], found=None) -> List[Dict[str, Any]]:
    if found is None:
        found = []
    
    if node.get("mimeType") == "video/mp4":
        found.append(node)
    
    for child in node.get("children", []):
        find_video_mp4_children(child, found)
    return found

async def fetch_vtt_for_video(client: httpx.AsyncClient, video_id: str, video_folder: Path) -> str:
    try:
        url = f"{TRANSCODER_STATS_URL}?resource_id={video_id}"
        resp = await client.get(url)
        resp.raise_for_status()
        stats_data = resp.json()
        
        vtt_urls = extract_vtt_urls(stats_data)
        combined_text = ""

        for vtt_url in vtt_urls:
            # Check for 'en' or 'english' in path
            if "/en/" in vtt_url.lower() or "/english/" in vtt_url.lower():
                try:
                    # Use a fresh client without headers for CDN links (avoids 401 on signed URLs)
                    async with httpx.AsyncClient() as cdn_client:
                        vtt_resp = await cdn_client.get(vtt_url)
                    
                    if vtt_resp.status_code == 200:
                        text = vtt_resp.text
                        filename = vtt_url.split("/")[-1]
                        (video_folder / "en").mkdir(exist_ok=True)
                        (video_folder / "en" / filename).write_text(text, encoding="utf-8")
                        combined_text += text + "\n"
                    else:
                        logger.warning(f"Failed to fetch VTT {vtt_url}: {vtt_resp.status_code}")
                except Exception as e:
                    logger.error(f"Error fetching VTT {vtt_url}: {e}")
        return combined_text
    except Exception as e:
        logger.warning(f"Failed to fetch VTT stats for {video_id}: {e}")
        return ""

def extract_vtt_urls(obj: Any, found=None) -> List[str]:
    if found is None:
        found = []
    
    if isinstance(obj, str) and obj.endswith(".vtt"):
        found.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            extract_vtt_urls(item, found)
    elif isinstance(obj, dict):
        for value in obj.values():
            extract_vtt_urls(value, found)
    return found

async def download_file(client: httpx.AsyncClient, url: str, path: Path):
    resp = await client.get(url)
    resp.raise_for_status()
    path.write_bytes(resp.content)

def strip_html(html_str: str) -> List[str]:
    """
    Cleans HTML and extracts list items if present, or just clean text lines.
    Returns a list of clean strings (Learning Objectives).
    """
    if not html_str:
        return []
        
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_str, "html.parser")
        
        list_items = soup.find_all('li')
        if list_items:
            objectives = [item.get_text(separator=' ', strip=True) for item in list_items]
            return [obj for obj in objectives if obj]
        else:
            text = soup.get_text(separator='\n', strip=True)
            return [line.strip() for line in text.split('\n') if line.strip()]
            
    except ImportError:
        import html
        text = html.unescape(html_str)
        li_matches = re.findall(r'<li>(.*?)</li>', text, re.IGNORECASE | re.DOTALL)
        if li_matches:
            cleaned = [re.sub(r'<[^>]+>', '', li).strip() for li in li_matches]
            return [c for c in cleaned if c]
            
        clean_text = re.sub(r'<[^>]+>', ' ', text)
        lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
        return lines

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()
