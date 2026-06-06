"""Integration tests for GET/POST /v1/pipeline/export|import endpoints."""
from __future__ import annotations

import io
import sqlite3
import time

import yaml
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upload(client: TestClient, data: dict, dry_run: bool = False) -> dict:
    text = yaml.dump(data, default_flow_style=False)
    url = "/v1/pipeline/import" + ("?dry_run=true" if dry_run else "")
    resp = client.post(url, files={"file": ("pipeline.yaml", text.encode(), "text/yaml")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_pipeline_config(tmp_db: str, key: str, value: str) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT OR REPLACE INTO pipeline_config (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, time.time()),
    )
    conn.commit()
    conn.close()


def _seed_feed_filter(tmp_db: str, ftype: str, fval: str) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT OR IGNORE INTO feed_filters (filter_type, match_value, created_at) VALUES (?, ?, ?)",
        (ftype, fval, time.time()),
    )
    conn.commit()
    conn.close()


def _seed_weight_rule(tmp_db: str, rtype: str, rval: str, mult: float) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT OR IGNORE INTO weight_rules (rule_type, match_value, multiplier, created_at) VALUES (?, ?, ?, ?)",
        (rtype, rval, mult, time.time()),
    )
    conn.commit()
    conn.close()


def _seed_item(tmp_db: str, scheme: str, external_id: str) -> int:
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT OR IGNORE INTO schemes (name, display_name, fields_json, created_at) VALUES (?,?,?,?)",
        (scheme, scheme, "[]", time.time()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO items (scheme, external_id, metadata_json, added_at) VALUES (?,?,?,?)",
        (scheme, external_id, "{}", time.time()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM items WHERE scheme=? AND external_id=?", (scheme, external_id)
    ).fetchone()
    conn.close()
    return row[0]


def _count_feed_filters(tmp_db: str) -> int:
    conn = sqlite3.connect(tmp_db)
    n = conn.execute("SELECT COUNT(*) FROM feed_filters").fetchone()[0]
    conn.close()
    return n


def _count_weight_rules(tmp_db: str) -> int:
    conn = sqlite3.connect(tmp_db)
    n = conn.execute("SELECT COUNT(*) FROM weight_rules").fetchone()[0]
    conn.close()
    return n


def _get_pipeline_config(tmp_db: str, key: str):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT value FROM pipeline_config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_yaml_content_type(self, client: TestClient):
        resp = client.get("/v1/pipeline/export")
        assert resp.status_code == 200
        assert "yaml" in resp.headers["content-type"]

    def test_export_yaml_content_disposition(self, client: TestClient):
        resp = client.get("/v1/pipeline/export")
        cd = resp.headers.get("content-disposition", "")
        assert "pipeline.yaml" in cd

    def test_export_yaml_is_parseable(self, client: TestClient):
        resp = client.get("/v1/pipeline/export")
        data = yaml.safe_load(resp.content)
        assert isinstance(data, dict)
        assert data["version"] == 1
        assert "exported_at" in data

    def test_export_yaml_has_required_sections(self, client: TestClient):
        resp = client.get("/v1/pipeline/export")
        data = yaml.safe_load(resp.content)
        for section in ("pipeline", "ppr", "sources", "feed_filters", "weight_rules",
                        "graphs", "custom_modules", "personas"):
            assert section in data, f"Missing section: {section}"

    def test_export_json_structure(self, client: TestClient):
        resp = client.get("/v1/pipeline/export/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        assert isinstance(data["sources"], dict)
        assert "lastfm" in data["sources"]

    def test_export_sources_have_enabled_and_weight(self, client: TestClient):
        resp = client.get("/v1/pipeline/export/json")
        sources = resp.json()["sources"]
        for name, cfg in sources.items():
            assert "enabled" in cfg, f"{name} missing enabled"
            assert "weight" in cfg, f"{name} missing weight"

    def test_export_sources_no_credentials(self, client: TestClient):
        import json as _json
        resp = client.get("/v1/pipeline/export")
        text = resp.text
        assert "credentials" not in text.lower()

    def test_export_pipeline_config_included(self, client: TestClient, tmp_db):
        _seed_pipeline_config(tmp_db, "scorer.ppr.weight", "1.5")
        resp = client.get("/v1/pipeline/export/json")
        data = resp.json()
        assert data["pipeline"].get("scorer.ppr.weight") == 1.5

    def test_export_feed_filters_included(self, client: TestClient, tmp_db):
        _seed_feed_filter(tmp_db, "channel_id", "UC_test")
        resp = client.get("/v1/pipeline/export/json")
        filters = resp.json()["feed_filters"]
        assert any(f["type"] == "channel_id" and f["value"] == "UC_test" for f in filters)

    def test_export_weight_rules_included(self, client: TestClient, tmp_db):
        _seed_weight_rule(tmp_db, "category", "tutorial", 1.5)
        resp = client.get("/v1/pipeline/export/json")
        rules = resp.json()["weight_rules"]
        assert any(r["type"] == "category" and r["value"] == "tutorial" for r in rules)

    def test_export_builtin_graphs_not_in_user_section(self, client: TestClient):
        resp = client.get("/v1/pipeline/export/json")
        graphs = resp.json()["graphs"]
        names = [g["name"] for g in graphs]
        assert "default" not in names
        assert "music" not in names
        assert "video" not in names


# ---------------------------------------------------------------------------
# Import — basic
# ---------------------------------------------------------------------------

class TestImport:
    def test_import_empty_payload_ok(self, client: TestClient):
        result = _upload(client, {"version": 1})
        assert result["ok"] is True

    def test_import_invalid_yaml_returns_400(self, client: TestClient):
        resp = client.post(
            "/v1/pipeline/import",
            files={"file": ("x.yaml", b"{ this: [is: broken", "text/yaml")},
        )
        assert resp.status_code == 400

    def test_import_non_mapping_returns_400(self, client: TestClient):
        resp = client.post(
            "/v1/pipeline/import",
            files={"file": ("x.yaml", b"- item1\n- item2\n", "text/yaml")},
        )
        assert resp.status_code == 400

    def test_import_returns_applied_counts(self, client: TestClient):
        result = _upload(client, {
            "version": 1,
            "pipeline": {"scorer.ppr.weight": 1.2},
            "feed_filters": [{"type": "category", "value": "spam"}],
        })
        assert result["applied"]["pipeline_config"] == 1
        assert result["applied"]["feed_filters"] == 1


# ---------------------------------------------------------------------------
# Import — pipeline_config
# ---------------------------------------------------------------------------

class TestImportPipelineConfig:
    def test_upserts_new_key(self, client: TestClient, tmp_db):
        _upload(client, {"pipeline": {"scorer.ppr.weight": 2.0}})
        assert _get_pipeline_config(tmp_db, "scorer.ppr.weight") == "2.0"

    def test_upserts_existing_key(self, client: TestClient, tmp_db):
        _seed_pipeline_config(tmp_db, "scorer.ppr.weight", "1.0")
        _upload(client, {"pipeline": {"scorer.ppr.weight": 1.75}})
        assert _get_pipeline_config(tmp_db, "scorer.ppr.weight") == "1.75"

    def test_boolean_value_stored(self, client: TestClient, tmp_db):
        _upload(client, {"pipeline": {"scorer.cosine.enabled": True}})
        assert _get_pipeline_config(tmp_db, "scorer.cosine.enabled") == "true"


# ---------------------------------------------------------------------------
# Import — sources
# ---------------------------------------------------------------------------

class TestImportSources:
    def test_updates_enabled_flag(self, client: TestClient, tmp_db):
        _upload(client, {"sources": {"deezer": {"enabled": False, "weight": 0.9}}})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT enabled, weight FROM sources WHERE name='deezer'").fetchone()
        conn.close()
        assert row[0] == 0
        assert abs(row[1] - 0.9) < 0.001

    def test_updates_weight(self, client: TestClient, tmp_db):
        _upload(client, {"sources": {"lastfm": {"enabled": True, "weight": 0.42}}})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT weight FROM sources WHERE name='lastfm'").fetchone()
        conn.close()
        assert abs(row[0] - 0.42) < 0.001

    def test_unknown_source_silently_skipped(self, client: TestClient, tmp_db):
        result = _upload(client, {"sources": {"does_not_exist_xyz": {"enabled": False}}})
        # applied count is the number of UPDATE attempts, not rows affected
        assert result["ok"] is True

    def test_credentials_not_written(self, client: TestClient, tmp_db):
        _upload(client, {"sources": {"lastfm": {"credentials": {"LASTFM_KEY": "leaked"}}}})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT credentials_json FROM sources WHERE name='lastfm'").fetchone()
        conn.close()
        assert row[0] is None or "leaked" not in (row[0] or "")


# ---------------------------------------------------------------------------
# Import — feed_filters
# ---------------------------------------------------------------------------

class TestImportFeedFilters:
    def test_replaces_existing_filters(self, client: TestClient, tmp_db):
        _seed_feed_filter(tmp_db, "channel_id", "UC_old")
        assert _count_feed_filters(tmp_db) == 1
        _upload(client, {"feed_filters": [
            {"type": "channel_id", "value": "UC_new1"},
            {"type": "category", "value": "spam"},
        ]})
        assert _count_feed_filters(tmp_db) == 2
        conn = sqlite3.connect(tmp_db)
        values = {r[0] for r in conn.execute("SELECT match_value FROM feed_filters").fetchall()}
        conn.close()
        assert "UC_old" not in values
        assert "UC_new1" in values

    def test_empty_list_clears_all_filters(self, client: TestClient, tmp_db):
        _seed_feed_filter(tmp_db, "channel_id", "UC_test")
        _upload(client, {"feed_filters": []})
        assert _count_feed_filters(tmp_db) == 0

    def test_malformed_entries_skipped(self, client: TestClient, tmp_db):
        _upload(client, {"feed_filters": [
            {"type": "channel_id", "value": "UC_good"},
            {"oops": "no type or value"},
            None,
        ]})
        assert _count_feed_filters(tmp_db) == 1


# ---------------------------------------------------------------------------
# Import — weight_rules
# ---------------------------------------------------------------------------

class TestImportWeightRules:
    def test_replaces_existing_rules(self, client: TestClient, tmp_db):
        _seed_weight_rule(tmp_db, "category", "old_cat", 2.0)
        _upload(client, {"weight_rules": [
            {"type": "category", "value": "tutorial", "multiplier": 1.5},
        ]})
        assert _count_weight_rules(tmp_db) == 1
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT match_value, multiplier FROM weight_rules").fetchone()
        conn.close()
        assert row[0] == "tutorial"
        assert abs(row[1] - 1.5) < 0.001

    def test_multiplier_defaults_to_1(self, client: TestClient, tmp_db):
        _upload(client, {"weight_rules": [{"type": "category", "value": "misc"}]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT multiplier FROM weight_rules").fetchone()
        conn.close()
        assert abs(row[0] - 1.0) < 0.001


# ---------------------------------------------------------------------------
# Import — graphs
# ---------------------------------------------------------------------------

class TestImportGraphs:
    def test_creates_new_user_graph(self, client: TestClient, tmp_db):
        _upload(client, {"graphs": [
            {"name": "My Music", "content_type": "music"},
        ]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT name, content_type FROM graphs WHERE name='My Music'").fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "music"

    def test_skips_duplicate_name(self, client: TestClient, tmp_db):
        _upload(client, {"graphs": [{"name": "Dup", "content_type": "mixed"}]})
        _upload(client, {"graphs": [{"name": "Dup", "content_type": "video"}]})
        conn = sqlite3.connect(tmp_db)
        count = conn.execute("SELECT COUNT(*) FROM graphs WHERE name='Dup'").fetchone()[0]
        row = conn.execute("SELECT content_type FROM graphs WHERE name='Dup'").fetchone()
        conn.close()
        assert count == 1
        assert row[0] == "mixed"  # second import was ignored

    def test_invalid_content_type_coerced_to_mixed(self, client: TestClient, tmp_db):
        _upload(client, {"graphs": [{"name": "Bad CT", "content_type": "unknown"}]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT content_type FROM graphs WHERE name='Bad CT'").fetchone()
        conn.close()
        assert row[0] == "mixed"


# ---------------------------------------------------------------------------
# Import — custom_modules
# ---------------------------------------------------------------------------

class TestImportCustomModules:
    def test_creates_new_module(self, client: TestClient, tmp_db):
        _upload(client, {"custom_modules": [
            {"name": "TestScorer", "type": "scorer", "enabled": True, "code": "return 1.0"},
        ]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT name, type, enabled, code FROM custom_modules WHERE name='TestScorer'").fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "scorer"
        assert row[2] == 1
        assert row[3] == "return 1.0"

    def test_updates_existing_module(self, client: TestClient, tmp_db):
        _upload(client, {"custom_modules": [
            {"name": "M1", "type": "scorer", "enabled": True, "code": "v1"},
        ]})
        _upload(client, {"custom_modules": [
            {"name": "M1", "type": "scorer", "enabled": False, "code": "v2"},
        ]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT enabled, code FROM custom_modules WHERE name='M1'").fetchone()
        conn.close()
        assert row[0] == 0
        assert row[1] == "v2"

    def test_missing_code_skipped(self, client: TestClient, tmp_db):
        _upload(client, {"custom_modules": [{"name": "NoCode", "type": "scorer"}]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT id FROM custom_modules WHERE name='NoCode'").fetchone()
        conn.close()
        assert row is None

    def test_invalid_type_skipped(self, client: TestClient, tmp_db):
        _upload(client, {"custom_modules": [
            {"name": "BadType", "type": "ranker", "code": "x"},
        ]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT id FROM custom_modules WHERE name='BadType'").fetchone()
        conn.close()
        assert row is None


# ---------------------------------------------------------------------------
# Import — personas
# ---------------------------------------------------------------------------

class TestImportPersonas:
    def test_creates_new_persona(self, client: TestClient, tmp_db):
        _upload(client, {"personas": [
            {"name": "Jazz", "alpha": 0.2, "min_seed_rating": 0, "seeds": []},
        ]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT name, alpha FROM personas WHERE name='Jazz'").fetchone()
        conn.close()
        assert row is not None
        assert abs(row[1] - 0.2) < 0.001

    def test_skips_existing_persona(self, client: TestClient, tmp_db):
        _upload(client, {"personas": [{"name": "P1", "alpha": 0.1, "seeds": []}]})
        _upload(client, {"personas": [{"name": "P1", "alpha": 0.9, "seeds": []}]})
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT alpha FROM personas WHERE name='P1'").fetchone()
        conn.close()
        assert abs(row[0] - 0.1) < 0.001  # first alpha preserved

    def test_seeds_added_when_item_exists(self, client: TestClient, tmp_db):
        _seed_item(tmp_db, "yt_video", "vid123")
        _upload(client, {"personas": [
            {"name": "WithSeed", "alpha": 0.15, "seeds": [
                {"scheme": "yt_video", "external_id": "vid123", "weight": 0.8},
            ]},
        ]})
        conn = sqlite3.connect(tmp_db)
        p_id = conn.execute("SELECT id FROM personas WHERE name='WithSeed'").fetchone()[0]
        seed_count = conn.execute(
            "SELECT COUNT(*) FROM persona_seeds WHERE persona_id=?", (p_id,)
        ).fetchone()[0]
        conn.close()
        assert seed_count == 1

    def test_seeds_skipped_when_item_missing(self, client: TestClient, tmp_db):
        _upload(client, {"personas": [
            {"name": "NoItem", "alpha": 0.15, "seeds": [
                {"scheme": "yt_video", "external_id": "does_not_exist", "weight": 1.0},
            ]},
        ]})
        conn = sqlite3.connect(tmp_db)
        p_id = conn.execute("SELECT id FROM personas WHERE name='NoItem'").fetchone()[0]
        seed_count = conn.execute(
            "SELECT COUNT(*) FROM persona_seeds WHERE persona_id=?", (p_id,)
        ).fetchone()[0]
        conn.close()
        assert seed_count == 0


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_flag_in_response(self, client: TestClient):
        result = _upload(client, {"pipeline": {"x.key": 99}}, dry_run=True)
        assert result["dry_run"] is True

    def test_dry_run_reports_counts(self, client: TestClient):
        result = _upload(client, {"pipeline": {"x.key": 1}}, dry_run=True)
        assert result["applied"]["pipeline_config"] == 1

    def test_dry_run_does_not_write_pipeline_config(self, client: TestClient, tmp_db):
        _upload(client, {"pipeline": {"dry.test.key": 42}}, dry_run=True)
        assert _get_pipeline_config(tmp_db, "dry.test.key") is None

    def test_dry_run_does_not_write_feed_filters(self, client: TestClient, tmp_db):
        _upload(client, {"feed_filters": [{"type": "category", "value": "drytest"}]}, dry_run=True)
        assert _count_feed_filters(tmp_db) == 0

    def test_dry_run_does_not_clear_existing_filters(self, client: TestClient, tmp_db):
        _seed_feed_filter(tmp_db, "channel_id", "UC_keep")
        _upload(client, {"feed_filters": []}, dry_run=True)
        assert _count_feed_filters(tmp_db) == 1

    def test_dry_run_does_not_create_persona(self, client: TestClient, tmp_db):
        _upload(client, {"personas": [{"name": "DryPersona", "seeds": []}]}, dry_run=True)
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT id FROM personas WHERE name='DryPersona'").fetchone()
        conn.close()
        assert row is None

    def test_real_import_after_dry_run_writes(self, client: TestClient, tmp_db):
        payload = {"pipeline": {"test.roundtrip": 7}}
        _upload(client, payload, dry_run=True)
        assert _get_pipeline_config(tmp_db, "test.roundtrip") is None
        _upload(client, payload, dry_run=False)
        assert _get_pipeline_config(tmp_db, "test.roundtrip") == "7"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_export_import_roundtrip_pipeline_config(self, client: TestClient, tmp_db):
        _seed_pipeline_config(tmp_db, "scorer.ppr.weight", "1.8")
        _seed_pipeline_config(tmp_db, "diversity.lambda", "0.6")

        export_resp = client.get("/v1/pipeline/export")
        assert export_resp.status_code == 200

        # Delete the keys
        conn = sqlite3.connect(tmp_db)
        conn.execute("DELETE FROM pipeline_config")
        conn.commit()
        conn.close()

        # Re-import
        client.post(
            "/v1/pipeline/import",
            files={"file": ("pipeline.yaml", export_resp.content, "text/yaml")},
        )

        assert _get_pipeline_config(tmp_db, "scorer.ppr.weight") == "1.8"
        assert _get_pipeline_config(tmp_db, "diversity.lambda") == "0.6"

    def test_export_import_roundtrip_feed_filters(self, client: TestClient, tmp_db):
        _seed_feed_filter(tmp_db, "channel_id", "UC_rt1")
        _seed_feed_filter(tmp_db, "category", "rt_spam")

        export_resp = client.get("/v1/pipeline/export")

        conn = sqlite3.connect(tmp_db)
        conn.execute("DELETE FROM feed_filters")
        conn.commit()
        conn.close()

        client.post(
            "/v1/pipeline/import",
            files={"file": ("pipeline.yaml", export_resp.content, "text/yaml")},
        )

        conn = sqlite3.connect(tmp_db)
        values = {r[0] for r in conn.execute("SELECT match_value FROM feed_filters").fetchall()}
        conn.close()
        assert "UC_rt1" in values
        assert "rt_spam" in values
