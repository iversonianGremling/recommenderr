import time

from fastapi import HTTPException

from backend.db import get_db

DEFAULT_GROUPS = {
    "existing": "Existing tags",
    "new": "New tags",
}
DEFAULT_GROUP_ORDER = ["existing", "new"]
VALID_TAG_KINDS = set(DEFAULT_GROUPS)


def _normalize_group_name(name: str | None) -> str:
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise HTTPException(status_code=422, detail="Music meta-tag name is required")
    return clean_name


def _normalize_tag_kind(kind: str | None, fallback: str | None = None) -> str:
    candidate = (kind or fallback or "existing").strip().lower()
    if candidate not in VALID_TAG_KINDS:
        raise HTTPException(status_code=422, detail="Music tag kind must be 'existing' or 'new'")
    return candidate


def _ensure_default_group(conn, system_key: str) -> int:
    normalized_key = _normalize_tag_kind(system_key)
    row = conn.execute(
        "SELECT id FROM music_tag_groups WHERE system_key=?",
        (normalized_key,),
    ).fetchone()
    if row:
        return int(row["id"])

    now = time.time()
    group_id = conn.execute(
        """
        INSERT INTO music_tag_groups (name, system_key, position, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            DEFAULT_GROUPS[normalized_key],
            normalized_key,
            DEFAULT_GROUP_ORDER.index(normalized_key),
            now,
            now,
        ),
    ).lastrowid
    return int(group_id)


def _ensure_default_groups(conn) -> dict[str, int]:
    return {
        system_key: _ensure_default_group(conn, system_key)
        for system_key in DEFAULT_GROUP_ORDER
    }


def _resolve_group_id(
    conn,
    group_id: int | None = None,
    legacy_kind: str | None = None,
    fallback_system_key: str = "existing",
) -> int:
    if group_id is not None:
        row = conn.execute("SELECT id FROM music_tag_groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Music meta-tag not found")
        return int(row["id"])
    return _ensure_default_group(conn, _normalize_tag_kind(legacy_kind, fallback_system_key))


def _legacy_kind_for_group(conn, group_id: int, fallback: str = "existing") -> str:
    row = conn.execute(
        "SELECT system_key FROM music_tag_groups WHERE id=?",
        (group_id,),
    ).fetchone()
    if row and row["system_key"] in VALID_TAG_KINDS:
        return str(row["system_key"])
    return _normalize_tag_kind(fallback)


def _next_group_position(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM music_tag_groups"
    ).fetchone()
    return int(row[0] or 0)


def _next_position(conn, parent_id: int | None, group_id: int | None = None) -> int:
    if parent_id is None:
        if group_id is None:
            raise HTTPException(status_code=422, detail="Target meta-tag is required for root tags")
        row = conn.execute(
            """
            SELECT COALESCE(MAX(position), -1) + 1
            FROM music_tags
            WHERE parent_id IS NULL AND group_id=?
            """,
            (group_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM music_tags WHERE parent_id=?",
            (parent_id,),
        ).fetchone()
    return int(row[0] or 0)


def _ordered_sibling_ids(
    conn,
    parent_id: int | None,
    group_id: int | None = None,
    exclude_id: int | None = None,
) -> list[int]:
    if parent_id is None:
        if group_id is None:
            raise HTTPException(status_code=422, detail="Target meta-tag is required for root tags")
        rows = conn.execute(
            """
            SELECT id
            FROM music_tags
            WHERE parent_id IS NULL
              AND group_id=?
              AND (? IS NULL OR id != ?)
            ORDER BY position ASC, id ASC
            """,
            (group_id, exclude_id, exclude_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id
            FROM music_tags
            WHERE parent_id=?
              AND (? IS NULL OR id != ?)
            ORDER BY position ASC, id ASC
            """,
            (parent_id, exclude_id, exclude_id),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def _renumber_parent(conn, parent_id: int | None, group_id: int | None = None):
    for index, tag_id in enumerate(_ordered_sibling_ids(conn, parent_id, group_id)):
        conn.execute("UPDATE music_tags SET position=? WHERE id=?", (index, tag_id))


def get_music_tag_descendant_ids(conn, tag_id: int) -> list[int]:
    rows = conn.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            SELECT id FROM music_tags WHERE id=?
            UNION ALL
            SELECT mt.id
            FROM music_tags mt
            JOIN descendants d ON mt.parent_id = d.id
        )
        SELECT id FROM descendants
        """,
        (tag_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _set_subtree_group(conn, tag_id: int, group_id: int):
    descendant_ids = get_music_tag_descendant_ids(conn, tag_id)
    if not descendant_ids:
        return
    placeholders = ",".join("?" for _ in descendant_ids)
    now = time.time()
    legacy_kind = _legacy_kind_for_group(conn, group_id)
    conn.execute(
        f"""
        UPDATE music_tags
        SET group_id=?, kind=?, updated_at=?
        WHERE id IN ({placeholders})
        """,
        [group_id, legacy_kind, now, *descendant_ids],
    )


def _move_tag(
    conn,
    tag_id: int,
    parent_id: int | None,
    position: int,
    group_id: int | None = None,
    kind: str | None = None,
):
    row = conn.execute(
        "SELECT id, parent_id, group_id, kind FROM music_tags WHERE id=?",
        (tag_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Music tag not found")

    current_parent_id = int(row["parent_id"]) if row["parent_id"] is not None else None
    current_group_id = (
        int(row["group_id"])
        if row["group_id"] is not None
        else _resolve_group_id(conn, legacy_kind=row["kind"], fallback_system_key="existing")
    )
    current_kind = _normalize_tag_kind(row["kind"], "existing")

    if parent_id == tag_id:
        raise HTTPException(status_code=422, detail="Tag cannot become its own parent")

    if parent_id is not None:
        parent = conn.execute(
            "SELECT id, group_id FROM music_tags WHERE id=?",
            (parent_id,),
        ).fetchone()
        if not parent:
            raise HTTPException(status_code=404, detail="Target parent not found")
        descendants = set(get_music_tag_descendant_ids(conn, tag_id))
        if parent_id in descendants:
            raise HTTPException(status_code=422, detail="Cannot move a tag into its own subtree")
        target_group_id = int(parent["group_id"]) if parent["group_id"] is not None else current_group_id
    else:
        if group_id is None and kind is None:
            target_group_id = current_group_id
        else:
            target_group_id = _resolve_group_id(
                conn,
                group_id=group_id,
                legacy_kind=kind,
                fallback_system_key=current_kind,
            )

    sibling_ids = _ordered_sibling_ids(
        conn,
        parent_id,
        target_group_id if parent_id is None else None,
        exclude_id=tag_id,
    )
    insert_at = max(0, min(position, len(sibling_ids)))
    sibling_ids.insert(insert_at, tag_id)

    now = time.time()
    target_kind = _legacy_kind_for_group(conn, target_group_id, current_kind)
    conn.execute(
        """
        UPDATE music_tags
        SET parent_id=?, group_id=?, kind=?, updated_at=?
        WHERE id=?
        """,
        (parent_id, target_group_id, target_kind, now, tag_id),
    )
    for index, sibling_id in enumerate(sibling_ids):
        if sibling_id == tag_id:
            conn.execute(
                """
                UPDATE music_tags
                SET parent_id=?, group_id=?, position=?, kind=?, updated_at=?
                WHERE id=?
                """,
                (parent_id, target_group_id, index, target_kind, now, sibling_id),
            )
        else:
            conn.execute(
                "UPDATE music_tags SET parent_id=?, position=?, updated_at=? WHERE id=?",
                (parent_id, index, now, sibling_id),
            )

    if target_group_id != current_group_id:
        _set_subtree_group(conn, tag_id, target_group_id)

    if current_parent_id is None:
        if current_group_id != target_group_id or parent_id is not None:
            _renumber_parent(conn, None, current_group_id)
    elif current_parent_id != parent_id:
        _renumber_parent(conn, current_parent_id)


def ensure_playlist_tag(conn, playlist_id: int, playlist_title: str | None) -> int:
    link = conn.execute(
        "SELECT tag_id FROM music_tag_playlist_links WHERE playlist_id=?",
        (playlist_id,),
    ).fetchone()
    if link:
        tag = conn.execute("SELECT id FROM music_tags WHERE id=?", (link["tag_id"],)).fetchone()
        if tag:
            return int(tag["id"])
        conn.execute("DELETE FROM music_tag_playlist_links WHERE playlist_id=?", (playlist_id,))

    default_group_id = _ensure_default_group(conn, "existing")
    now = time.time()
    tag_id = conn.execute(
        """
        INSERT INTO music_tags (name, kind, group_id, parent_id, position, created_at, updated_at)
        VALUES (?, 'existing', ?, NULL, ?, ?, ?)
        """,
        (
            (playlist_title or "").strip() or f"Playlist {playlist_id}",
            default_group_id,
            _next_position(conn, None, default_group_id),
            now,
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO music_tag_playlist_links (playlist_id, tag_id, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (playlist_id, tag_id, now, now),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at)
        SELECT ?, ml.video_id, ?
        FROM music_library ml
        JOIN music_jobs mj ON mj.id = ml.source_job_id
        WHERE mj.playlist_id=?
        """,
        (tag_id, now, playlist_id),
    )
    return int(tag_id)


def ensure_all_playlist_tags(conn):
    _ensure_default_groups(conn)
    rows = conn.execute(
        """
        SELECT playlist_id, MAX(playlist_title) AS playlist_title
        FROM music_jobs
        WHERE playlist_id IS NOT NULL
        GROUP BY playlist_id
        ORDER BY MIN(created_at) ASC
        """
    ).fetchall()
    for row in rows:
        ensure_playlist_tag(conn, int(row["playlist_id"]), row["playlist_title"])


def _sync_tag_playlist_library_entries(conn, tag_id: int | None = None):
    params: list[object] = [time.time()]
    where = ""
    if tag_id is not None:
        where = "WHERE l.tag_id = ?"
        params.append(tag_id)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO music_library (
            video_id,
            title,
            thumbnail,
            duration,
            author,
            author_id,
            source_job_id,
            source_video_id,
            added_at
        )
        SELECT DISTINCT
            pv.video_id,
            COALESCE(NULLIF(TRIM(pv.title), ''), pv.video_id),
            pv.thumbnail,
            pv.duration,
            pv.author,
            pv.author_id,
            latest_jobs.job_id,
            pv.video_id,
            ?
        FROM music_tag_playlist_links l
        JOIN playlist_videos pv ON pv.playlist_id = l.playlist_id
        LEFT JOIN (
            SELECT playlist_id, MAX(id) AS job_id
            FROM music_jobs
            WHERE playlist_id IS NOT NULL
            GROUP BY playlist_id
        ) latest_jobs ON latest_jobs.playlist_id = l.playlist_id
        {where}
        """,
        params,
    )


def _sync_tag_playlist_assignments(conn, tag_id: int | None = None):
    params: list[object] = [time.time()]
    where = ""
    if tag_id is not None:
        where = "WHERE l.tag_id = ?"
        params.append(tag_id)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at)
        SELECT DISTINCT l.tag_id, ml.video_id, ?
        FROM music_tag_playlist_links l
        JOIN playlist_videos pv ON pv.playlist_id = l.playlist_id
        JOIN music_library ml
          ON ml.video_id = pv.video_id
          OR ml.source_video_id = pv.video_id
        {where}
        """,
        params,
    )


def sync_tag_playlist_content(conn, tag_id: int | None = None):
    _sync_tag_playlist_library_entries(conn, tag_id)
    _sync_tag_playlist_assignments(conn, tag_id)


def list_music_tag_groups():
    conn = get_db()
    try:
        _ensure_default_groups(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT
                g.id,
                g.name,
                g.system_key,
                g.position,
                COALESCE(stats.root_count, 0) AS root_count,
                COALESCE(stats.tag_count, 0) AS tag_count
            FROM music_tag_groups g
            LEFT JOIN (
                SELECT
                    group_id,
                    COUNT(*) AS tag_count,
                    SUM(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) AS root_count
                FROM music_tags
                GROUP BY group_id
            ) stats ON stats.group_id = g.id
            ORDER BY g.position ASC, g.id ASC
            """
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "system_key": row["system_key"],
                "position": int(row["position"]),
                "root_count": int(row["root_count"] or 0),
                "tag_count": int(row["tag_count"] or 0),
            }
            for row in rows
        ]
    finally:
        conn.close()


def list_music_tags():
    conn = get_db()
    try:
        default_group_ids = _ensure_default_groups(conn)
        ensure_all_playlist_tags(conn)
        sync_tag_playlist_content(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT
                mt.id,
                mt.name,
                mt.kind,
                mt.group_id,
                g.name AS group_name,
                g.system_key AS group_system_key,
                g.position AS group_position,
                mt.parent_id,
                mt.position,
                COALESCE(COUNT(DISTINCT mta.video_id), 0) AS direct_item_count
            FROM music_tags mt
            LEFT JOIN music_tag_groups g ON g.id = mt.group_id
            LEFT JOIN music_tag_assignments mta ON mta.tag_id = mt.id
            GROUP BY mt.id
            ORDER BY COALESCE(g.position, 0) ASC, mt.position ASC, mt.id ASC
            """
        ).fetchall()
        playlist_count_rows = conn.execute(
            """
            SELECT tag_id, COUNT(*) AS playlist_count
            FROM music_tag_playlist_links
            GROUP BY tag_id
            """
        ).fetchall()
    finally:
        conn.close()

    playlist_counts = {int(row["tag_id"]): int(row["playlist_count"]) for row in playlist_count_rows}
    nodes = {}
    for row in rows:
        legacy_kind = _normalize_tag_kind(row["kind"], row["group_system_key"] or "existing")
        group_id = (
            int(row["group_id"])
            if row["group_id"] is not None
            else default_group_ids.get(legacy_kind, default_group_ids["existing"])
        )
        group_name = row["group_name"] or DEFAULT_GROUPS.get(legacy_kind, DEFAULT_GROUPS["existing"])
        nodes[int(row["id"])] = {
            "id": int(row["id"]),
            "name": row["name"],
            "kind": legacy_kind,
            "group_id": group_id,
            "group_name": group_name,
            "group_system_key": row["group_system_key"],
            "parent_id": int(row["parent_id"]) if row["parent_id"] is not None else None,
            "position": int(row["position"]),
            "direct_item_count": int(row["direct_item_count"] or 0),
            "item_count": int(row["direct_item_count"] or 0),
            "playlist_count": playlist_counts.get(int(row["id"]), 0),
            "children": [],
            "_group_position": int(row["group_position"] or 0),
        }

    roots: list[dict] = []
    for node in nodes.values():
        parent_id = node["parent_id"]
        if parent_id is None or parent_id not in nodes:
            roots.append(node)
            continue
        nodes[parent_id]["children"].append(node)

    def finalize(node: dict) -> int:
        node["children"].sort(key=lambda child: (child["position"], child["name"].lower(), child["id"]))
        total = node["direct_item_count"]
        for child in node["children"]:
            total += finalize(child)
        node["item_count"] = total
        node.pop("_group_position", None)
        return total

    roots.sort(key=lambda node: (node["_group_position"], node["position"], node["name"].lower(), node["id"]))
    for root in roots:
        finalize(root)
    return roots


def create_music_tag_group(name: str) -> int:
    clean_name = _normalize_group_name(name)
    conn = get_db()
    try:
        _ensure_default_groups(conn)
        duplicate = conn.execute(
            "SELECT id FROM music_tag_groups WHERE lower(name)=lower(?)",
            (clean_name,),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=422, detail="Music meta-tag name already exists")

        now = time.time()
        group_id = conn.execute(
            """
            INSERT INTO music_tag_groups (name, system_key, position, created_at, updated_at)
            VALUES (?, NULL, ?, ?, ?)
            """,
            (clean_name, _next_group_position(conn), now, now),
        ).lastrowid
        conn.commit()
        return int(group_id)
    finally:
        conn.close()


def update_music_tag_group(group_id: int, name: str):
    clean_name = _normalize_group_name(name)
    conn = get_db()
    try:
        group = conn.execute(
            "SELECT id FROM music_tag_groups WHERE id=?",
            (group_id,),
        ).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="Music meta-tag not found")

        duplicate = conn.execute(
            "SELECT id FROM music_tag_groups WHERE lower(name)=lower(?) AND id != ?",
            (clean_name, group_id),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=422, detail="Music meta-tag name already exists")

        conn.execute(
            "UPDATE music_tag_groups SET name=?, updated_at=? WHERE id=?",
            (clean_name, time.time(), group_id),
        )
        conn.commit()
    finally:
        conn.close()


def create_music_tag(
    name: str,
    parent_id: int | None = None,
    group_id: int | None = None,
    kind: str = "new",
) -> int:
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise HTTPException(status_code=422, detail="Music tag name is required")

    conn = get_db()
    try:
        _ensure_default_groups(conn)
        target_group_id = _resolve_group_id(conn, group_id=group_id, legacy_kind=kind, fallback_system_key="new")
        if parent_id is not None:
            parent = conn.execute(
                "SELECT id, group_id FROM music_tags WHERE id=?",
                (parent_id,),
            ).fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="Parent music tag not found")
            target_group_id = int(parent["group_id"]) if parent["group_id"] is not None else target_group_id

        now = time.time()
        tag_id = conn.execute(
            """
            INSERT INTO music_tags (name, kind, group_id, parent_id, position, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_name,
                _legacy_kind_for_group(conn, target_group_id, kind),
                target_group_id,
                parent_id,
                _next_position(conn, parent_id, target_group_id),
                now,
                now,
            ),
        ).lastrowid
        conn.commit()
        return int(tag_id)
    finally:
        conn.close()


def ensure_slash_genre_subtag(conn, genre_label: str) -> None:
    """If ``genre_label`` looks like ``Parent/Child``, ensure ``Child`` exists under root ``Parent`` in the Genres meta-tag group."""
    raw = " ".join((genre_label or "").split())
    if "/" not in raw:
        return
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if len(parts) != 2:
        return
    parent_name, child_name = parts[0], parts[1]
    _ensure_default_groups(conn)
    genre_group_id = None
    for row in conn.execute(
        "SELECT id, name FROM music_tag_groups ORDER BY position ASC, id ASC"
    ).fetchall():
        n = (row["name"] or "").strip().lower()
        if n == "genres" or n == "genre" or "genres" in n:
            genre_group_id = int(row["id"])
            break
    if genre_group_id is None:
        return
    parent = conn.execute(
        """
        SELECT id, group_id, kind FROM music_tags
        WHERE group_id=? AND parent_id IS NULL AND lower(trim(name))=lower(?)
        """,
        (genre_group_id, parent_name),
    ).fetchone()
    if not parent:
        return
    parent_id = int(parent["id"])
    if conn.execute(
        "SELECT 1 FROM music_tags WHERE parent_id=? AND lower(trim(name))=lower(?)",
        (parent_id, child_name),
    ).fetchone():
        return
    now = time.time()
    gid = int(parent["group_id"]) if parent["group_id"] is not None else _ensure_default_group(conn, "existing")
    legacy_kind = _legacy_kind_for_group(
        conn,
        gid,
        _normalize_tag_kind(parent["kind"], "existing"),
    )
    conn.execute(
        """
        INSERT INTO music_tags (name, kind, group_id, parent_id, position, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            child_name,
            legacy_kind,
            gid,
            parent_id,
            _next_position(conn, parent_id),
            now,
            now,
        ),
    )


def move_music_tag(
    tag_id: int,
    parent_id: int | None,
    position: int,
    group_id: int | None = None,
    kind: str | None = None,
):
    conn = get_db()
    try:
        _move_tag(conn, tag_id, parent_id, position, group_id, kind)
        conn.commit()
    finally:
        conn.close()


def merge_music_tag(source_id: int, target_id: int, preserve_source: bool):
    if source_id == target_id:
        raise HTTPException(status_code=422, detail="Choose a different target tag")

    conn = get_db()
    try:
        _ensure_default_groups(conn)
        source = conn.execute(
            "SELECT id, parent_id, group_id, kind FROM music_tags WHERE id=?",
            (source_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT id, group_id FROM music_tags WHERE id=?",
            (target_id,),
        ).fetchone()
        if not source or not target:
            raise HTTPException(status_code=404, detail="Music tag not found")

        descendants = set(get_music_tag_descendant_ids(conn, source_id))
        if target_id in descendants:
            raise HTTPException(status_code=422, detail="Cannot merge into a descendant tag")

        if preserve_source:
            _move_tag(conn, source_id, target_id, _next_position(conn, target_id))
            conn.commit()
            return

        source_group_id = (
            int(source["group_id"])
            if source["group_id"] is not None
            else _resolve_group_id(conn, legacy_kind=source["kind"], fallback_system_key="existing")
        )
        target_group_id = (
            int(target["group_id"])
            if target["group_id"] is not None
            else source_group_id
        )

        now = time.time()
        target_kind = _legacy_kind_for_group(conn, target_group_id, "existing")
        source_children = _ordered_sibling_ids(conn, source_id)
        next_position = _next_position(conn, target_id)
        for child_id in source_children:
            conn.execute(
                """
                UPDATE music_tags
                SET parent_id=?, position=?, group_id=?, kind=?, updated_at=?
                WHERE id=?
                """,
                (target_id, next_position, target_group_id, target_kind, now, child_id),
            )
            _set_subtree_group(conn, child_id, target_group_id)
            next_position += 1

        conn.execute(
            """
            INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at)
            SELECT ?, video_id, ?
            FROM music_tag_assignments
            WHERE tag_id=?
            """,
            (target_id, now, source_id),
        )
        conn.execute(
            """
            INSERT INTO music_tag_playlist_links (playlist_id, tag_id, created_at, updated_at)
            SELECT playlist_id, ?, created_at, ?
            FROM music_tag_playlist_links
            WHERE tag_id=?
            ON CONFLICT(playlist_id) DO UPDATE SET
                tag_id=excluded.tag_id,
                updated_at=excluded.updated_at
            """,
            (target_id, now, source_id),
        )
        sync_tag_playlist_content(conn, target_id)
        conn.execute("DELETE FROM music_tags WHERE id=?", (source_id,))

        if source["parent_id"] is None:
            _renumber_parent(conn, None, source_group_id)
        else:
            _renumber_parent(conn, int(source["parent_id"]))
        _renumber_parent(conn, target_id)
        conn.commit()
    finally:
        conn.close()


def manual_upsert_music_library_row(
    conn,
    *,
    video_id: str,
    title: str,
    thumbnail: str | None,
    duration: int | None,
    author: str | None,
    author_id: str | None,
    track: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
) -> None:
    """Insert or update a library row for manual tagging (no music_jobs source)."""
    now = time.time()
    safe_title = (title or "").strip() or video_id
    conn.execute(
        """
        INSERT INTO music_library (
            video_id, title, thumbnail, duration, author, author_id,
            track, artist, album, genre, source_job_id, source_video_id, added_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
            title=COALESCE(NULLIF(TRIM(excluded.title), ''), music_library.title),
            thumbnail=COALESCE(excluded.thumbnail, music_library.thumbnail),
            duration=COALESCE(excluded.duration, music_library.duration),
            author=COALESCE(NULLIF(TRIM(excluded.author), ''), music_library.author),
            author_id=COALESCE(NULLIF(TRIM(excluded.author_id), ''), music_library.author_id),
            track=COALESCE(NULLIF(TRIM(excluded.track), ''), music_library.track),
            artist=COALESCE(NULLIF(TRIM(excluded.artist), ''), music_library.artist),
            album=COALESCE(NULLIF(TRIM(excluded.album), ''), music_library.album),
            genre=COALESCE(NULLIF(TRIM(excluded.genre), ''), music_library.genre),
            source_video_id=COALESCE(music_library.source_video_id, excluded.source_video_id)
        """,
        (
            video_id,
            safe_title,
            thumbnail,
            duration,
            author,
            author_id,
            track,
            artist,
            album,
            genre,
            video_id,
            now,
        ),
    )


def rename_music_tag(tag_id: int, name: str):
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise HTTPException(status_code=422, detail="Tag name cannot be empty")
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM music_tags WHERE id=?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Music tag not found")
        conn.execute(
            "UPDATE music_tags SET name=?, updated_at=? WHERE id=?",
            (clean_name, time.time(), tag_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_music_tag(tag_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM music_tags WHERE id=?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Music tag not found")
        # Re-parent children to the deleted tag's parent
        parent = conn.execute("SELECT parent_id FROM music_tags WHERE id=?", (tag_id,)).fetchone()
        parent_id = parent["parent_id"] if parent else None
        conn.execute("UPDATE music_tags SET parent_id=? WHERE parent_id=?", (parent_id, tag_id))
        # Remove assignments and the tag itself
        conn.execute("DELETE FROM music_tag_assignments WHERE tag_id=?", (tag_id,))
        conn.execute("DELETE FROM music_tags WHERE id=?", (tag_id,))
        conn.commit()
    finally:
        conn.close()


def manual_assign_music_tags(conn, video_id: str, tag_ids: list[int]) -> None:
    now = time.time()
    for tag_id in tag_ids:
        row = conn.execute("SELECT id FROM music_tags WHERE id=?", (tag_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown music tag id {tag_id}")
        conn.execute(
            """
            INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at)
            VALUES (?, ?, ?)
            """,
            (tag_id, video_id, now),
        )
