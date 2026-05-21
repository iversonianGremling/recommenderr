import time
from backend.db import get_db


DEFAULT_CATEGORIES = {
    "Music": ["music", "song", "album", "concert", "remix", "cover", "lyrics", "beat", "playlist", "official audio", "official video", "ft.", "feat."],
    "Gaming": ["gaming", "gameplay", "playthrough", "walkthrough", "lets play", "let's play", "speedrun", "esports", "gamer", "xbox", "playstation", "nintendo"],
    "Education": ["tutorial", "lecture", "course", "learn", "explained", "how to", "lesson", "educational"],
    "Science & Tech": ["science", "technology", "engineering", "physics", "chemistry", "coding", "programming", "ai ", "machine learning", "software", "hardware", "linux", "python", "javascript"],
    "News & Politics": ["news", "politics", "election", "debate", "report", "breaking", "journalist"],
    "Entertainment": ["comedy", "funny", "sketch", "prank", "reaction", "standup", "stand-up", "roast"],
    "Sports": ["sports", "football", "basketball", "soccer", "nba", "nfl", "goal", "highlights", "match", "boxing", "mma", "ufc"],
    "Film & Animation": ["movie", "film", "animation", "trailer", "anime", "animated", "short film", "cinema", "review"],
    "Howto & Style": ["diy", "recipe", "cooking", "fashion", "style", "hack", "tips", "guide", "how-to"],
    "Essays": ["essay", "analysis", "deep dive", "video essay", "retrospective", "commentary", "critique", "examination"],
}

# Maps YouTube's genre strings to our category names
_YT_GENRE_MAP = {
    "Music": "Music",
    "Gaming": "Gaming",
    "Education": "Education",
    "Science & Technology": "Science & Tech",
    "News & Politics": "News & Politics",
    "Comedy": "Entertainment",
    "Entertainment": "Entertainment",
    "Sports": "Sports",
    "Film & Animation": "Film & Animation",
    "Howto & Style": "Howto & Style",
    "Autos & Vehicles": "Howto & Style",
    "Travel & Events": "Entertainment",
    "People & Blogs": "Entertainment",
    "Pets & Animals": "Entertainment",
    "Nonprofits & Activism": "News & Politics",
}


def _get_custom_categories():
    conn = get_db()
    rows = conn.execute("SELECT name, keywords FROM custom_categories").fetchall()
    conn.close()
    return {r["name"]: [k.strip().lower() for k in r["keywords"].split(",") if k.strip()] for r in rows}


def _map_yt_genre(genre: str) -> str:
    return _YT_GENRE_MAP.get(genre, "Uncategorized")


def categorize_video(title, author="", extra_text=""):
    """Classify by keyword matching against title + author + extra_text (e.g. stored keywords)."""
    text = f"{title} {author} {extra_text}".lower()

    all_categories = dict(DEFAULT_CATEGORIES)
    all_categories.update(_get_custom_categories())

    best_category = "Uncategorized"
    best_count = 0

    for category, keywords in all_categories.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > best_count:
            best_count = count
            best_category = category

    return best_category


def categorize_from_stored_data(video_id: str, title: str, author: str) -> str:
    """Use stored keywords + genre from video_metadata for richer classification.
    Falls back to title-only matching, then YouTube's own genre string.
    """
    conn = get_db()
    meta = conn.execute("SELECT genre FROM video_metadata WHERE video_id = ?", (video_id,)).fetchone()
    kw_rows = conn.execute("SELECT keyword FROM video_keywords WHERE video_id = ?", (video_id,)).fetchall()
    conn.close()

    keywords_text = " ".join(r["keyword"] for r in kw_rows)
    genre = meta["genre"] if meta else None

    result = categorize_video(title, author, keywords_text)
    if result != "Uncategorized":
        return result

    if genre:
        return _map_yt_genre(genre)

    return "Uncategorized"


def bulk_categorize(videos):
    """Categorize multiple videos, skipping already-categorized ones (source != 'user').

    Uses stored keywords+genre if available, falls back to title matching.
    Args:
        videos: list of dicts with video_id, title, author keys
    """
    if not videos:
        return

    conn = get_db()
    now = time.time()

    video_ids = [v["video_id"] for v in videos]
    placeholders = ",".join("?" * len(video_ids))
    # Don't overwrite user-set categories
    existing = conn.execute(
        f"SELECT video_id FROM video_categories WHERE video_id IN ({placeholders})",
        video_ids
    ).fetchall()
    existing_ids = {r["video_id"] for r in existing}

    # Fetch stored metadata for all videos at once
    meta_rows = conn.execute(
        f"SELECT video_id, genre FROM video_metadata WHERE video_id IN ({placeholders})",
        video_ids
    ).fetchall()
    genre_map = {r["video_id"]: r["genre"] for r in meta_rows}

    kw_rows = conn.execute(
        f"SELECT video_id, keyword FROM video_keywords WHERE video_id IN ({placeholders})",
        video_ids
    ).fetchall()
    kw_map: dict[str, list[str]] = {}
    for r in kw_rows:
        kw_map.setdefault(r["video_id"], []).append(r["keyword"])

    conn.close()

    conn = get_db()
    for v in videos:
        vid = v["video_id"]
        if vid in existing_ids:
            continue
        kw_text = " ".join(kw_map.get(vid, []))
        category = categorize_video(v.get("title", ""), v.get("author", ""), kw_text)
        if category == "Uncategorized" and genre_map.get(vid):
            category = _map_yt_genre(genre_map[vid])
        conn.execute(
            "INSERT OR IGNORE INTO video_categories (video_id, category, source, updated_at) VALUES (?,?,?,?)",
            (vid, category, "auto", now)
        )

    conn.commit()
    conn.close()


def get_all_categories():
    """Return flat sorted list of all known category paths."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT category FROM video_categories
        UNION
        SELECT name FROM custom_categories
        ORDER BY 1
    """).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_category_tree():
    """Return hierarchical category structure.

    Returns a list of top-level nodes, each with:
      { name, path, children: [...] }
    where path uses '/' as separator (e.g. 'Music/Russian Post Punk').
    """
    all_cats = get_all_categories()
    # Build tree from paths
    tree: dict = {}
    for path in all_cats:
        parts = path.split("/")
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]

    def _to_list(node: dict, prefix: str) -> list:
        result = []
        for name, children in sorted(node.items()):
            path = f"{prefix}/{name}" if prefix else name
            result.append({
                "name": name,
                "path": path,
                "children": _to_list(children, path),
            })
        return result

    return _to_list(tree, "")


def set_video_category(video_id, category):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO video_categories (video_id, category, source, updated_at) VALUES (?,?,?,?)",
        (video_id, category, "user", time.time())
    )
    conn.commit()
    conn.close()


def add_custom_category(name, keywords=""):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO custom_categories (name, keywords, created_at) VALUES (?,?,?)",
        (name, keywords, time.time())
    )
    conn.commit()
    conn.close()


def remove_custom_category(name):
    conn = get_db()
    conn.execute("DELETE FROM custom_categories WHERE name = ?", (name,))
    conn.commit()
    conn.close()
