import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query
import yt_dlp

logger = logging.getLogger("comments")

router = APIRouter()


def _fetch_comments(video_id: str, max_comments: int = 30) -> list:
    logger.info(f"[comments] fetching for {video_id} (max={max_comments})")
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": [str(max_comments)]}},
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs": "github"},
        }) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            raw = info.get("comments", [])
            logger.info(f"[comments] {video_id}: got {len(raw)} comments")
            return [
                {
                    "author": c.get("author", "Unknown"),
                    "author_id": c.get("author_id", ""),
                    "author_thumbnail": c.get("author_thumbnail", ""),
                    "text": c.get("text", ""),
                    "likes": c.get("like_count", 0),
                    "time_text": c.get("_time_text", c.get("timestamp", "")),
                    "is_pinned": c.get("is_pinned", False),
                    "is_hearted": c.get("is_favorited", False),
                }
                for c in raw
            ]
    except Exception as e:
        logger.error(f"[comments] {video_id} failed: {e}")
        raise


@router.get("/{video_id}/comments")
async def get_comments(video_id: str, max: int = Query(30)):
    try:
        loop = asyncio.get_event_loop()
        comments = await loop.run_in_executor(None, _fetch_comments, video_id, max)
        return {"comments": comments, "count": len(comments)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch comments: {e}")
