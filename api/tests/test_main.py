import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.main as main


class FakeB2:
    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.test/{Params['Key']}?ttl={ExpiresIn}"


class FakeThread:
    def __init__(self, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        return None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.old_runs = main.RUNS
        self.old_runtime = main.RUNTIME_DIR
        main.RUNS = {}
        main.RUNTIME_DIR = self.runtime
        self.env = patch.dict(
            os.environ,
            {
                "B2_BUCKET": "asset-bucket",
                "B2_REGION": "us-west-004",
                "B2_KEY_ID": "test-id",
                "B2_APP_KEY": "test-key",
                "ANTHROPIC_API_KEY": "test-claude",
                "GMI_API_KEY": "test-gmi",
                "MAX_VARIANTS_PER_RUN": "12",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        main.RUNS = self.old_runs
        main.RUNTIME_DIR = self.old_runtime
        self.temp.cleanup()

    def test_create_run_ingests_source_and_queues_real_combinations(self):
        fake_b2 = FakeB2()
        with (
            patch.object(main, "b2_client", return_value=fake_b2),
            patch.object(main.threading, "Thread", FakeThread),
        ):
            response = TestClient(main.app).post(
                "/api/runs",
                files={"file": ("product.png", b"\x89PNG\r\n\x1a\nreal-image-bytes", "image/png")},
                data={
                    "product_name": "Porcelain tea set",
                    "brief": "Preserve all three pieces and floral pattern.",
                    "markets": json.dumps(["Japan", "France"]),
                    "channels": json.dumps(["Editorial"]),
                    "environments": json.dumps(["Studio", "Night"]),
                    "aesthetic": "Quiet luxury",
                },
            )

        self.assertEqual(response.status_code, 202, response.text)
        run = response.json()
        self.assertEqual(len(run["variants"]), 4)
        self.assertEqual(run["source_sha256"], hashlib.sha256(b"\x89PNG\r\n\x1a\nreal-image-bytes").hexdigest())
        self.assertTrue(run["source_key"].endswith("/original.png"))
        self.assertEqual(fake_b2.objects[0]["ContentType"], "image/png")
        self.assertTrue((self.runtime / f"{run['run_id']}.json").exists())

    def test_create_run_rejects_empty_selection_before_generation(self):
        fake_b2 = FakeB2()
        with (
            patch.object(main, "b2_client", return_value=fake_b2),
            patch.object(main.threading, "Thread", FakeThread),
        ):
            response = TestClient(main.app).post(
                "/api/runs",
                files={"file": ("product.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
                data={
                    "product_name": "Tea set",
                    "brief": "Preserve the pattern.",
                    "markets": "[]",
                    "channels": json.dumps(["Editorial"]),
                    "environments": json.dumps(["Studio"]),
                    "aesthetic": "Quiet luxury",
                },
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(fake_b2.objects, [])

    def test_execute_variant_commits_only_verified_real_asset(self):
        run_id = "offline-run"
        variant = main.Variant("v1", "Studio / Editorial", "France", "Editorial", "Studio")
        main.RUNS[run_id] = {"run_id": run_id, "status": "running", "source_key": "source.png", "variants": [main.asdict(variant)]}
        asset = SimpleNamespace(
            url="https://s3.us-west-004.backblazeb2.com/asset-bucket/output.png",
            sha256="abc123",
        )
        manifest = SimpleNamespace(verify=lambda: True, manifest_uri="b2://asset-bucket/manifest.json")
        result = SimpleNamespace(
            run=SimpleNamespace(steps=[SimpleNamespace(assets=[asset])]),
            manifest=manifest,
        )

        class FakePipeline:
            def __init__(self, *args, **kwargs):
                pass

            def step(self, *args, **kwargs):
                self.params = kwargs
                return self

            def run(self, **kwargs):
                return result

        with (
            patch.object(main, "provider_for", return_value=(object(), "seedream-5.0-lite", "GMI Cloud / Seedream 5")),
            patch.object(main, "direct_variant_with_claude", return_value="high-end prompt"),
            patch.object(main, "Pipeline", FakePipeline),
            patch.object(main, "storage_sink", return_value=object()),
            patch.object(main, "presign_output_url", return_value="https://signed.test/output.png"),
            patch.object(main, "evaluate_variant_with_claude", return_value={
                "score": 96, "notes": "Identity preserved.", "critical_drift": False,
                "violations": [], "axes": {axis: 96 for axis in main.QA_AXES},
                "failed_constraint_ids": [], "repair_plan": {"retry_recommended": False},
            }),
        ):
            manifest_urls = main.execute_variant(
                run_id,
                "https://signed.test/source.png",
                "Tea set",
                "identity",
                "Quiet luxury",
                "image/png",
                "source-sha",
                variant,
            )

        saved = main.RUNS[run_id]["variants"][0]
        self.assertEqual(manifest_urls, ["b2://asset-bucket/manifest.json"])
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["score"], 96)
        self.assertEqual(saved["durable_url"], asset.url)
        self.assertEqual(saved["url"], "https://signed.test/output.png")
        self.assertEqual(saved["attempts"][0]["outcome"], "accepted")

    def test_low_identity_score_retries_then_flags(self):
        run_id = "retry-run"
        variant = main.Variant("v1", "Studio / Editorial", "France", "Editorial", "Studio")
        main.RUNS[run_id] = {"run_id": run_id, "status": "running", "source_key": "source.png", "variants": [main.asdict(variant)]}
        calls = {"pipeline": 0}

        class FakePipeline:
            def __init__(self, *args, **kwargs):
                pass

            def step(self, *args, **kwargs):
                return self

            def run(self, **kwargs):
                calls["pipeline"] += 1
                asset = SimpleNamespace(
                    url=f"https://s3.us-west-004.backblazeb2.com/asset-bucket/output-{calls['pipeline']}.png",
                    sha256=f"sha-{calls['pipeline']}",
                )
                return SimpleNamespace(
                    run=SimpleNamespace(steps=[SimpleNamespace(assets=[asset])]),
                    manifest=SimpleNamespace(
                        verify=lambda: True,
                        manifest_uri=f"b2://asset-bucket/manifest-{calls['pipeline']}.json",
                    ),
                )

        with (
            patch.dict(os.environ, {"IDENTITY_QA_THRESHOLD": "85", "IDENTITY_MAX_ATTEMPTS": "2"}),
            patch.object(main, "provider_for", return_value=(object(), "seedream-5.0-lite", "GMI Cloud / Seedream 5")),
            patch.object(main, "direct_variant_with_claude", return_value="base prompt"),
            patch.object(main, "Pipeline", FakePipeline),
            patch.object(main, "storage_sink", return_value=object()),
            patch.object(main, "presign_output_url", side_effect=lambda url: f"{url}?signed=1"),
            patch.object(main, "evaluate_variant_with_claude", side_effect=[
                {
                    "score": 40, "notes": "Handle changed.", "critical_drift": True,
                    "violations": ["handle geometry changed"], "axes": {"geometry": 40},
                    "failed_constraint_ids": ["GEO-01"],
                    "repair_plan": {"retry_recommended": True, "repair_instruction": "Restore handle geometry."},
                },
                {
                    "score": 70, "notes": "Pattern drift.", "critical_drift": True,
                    "violations": ["pattern changed"], "axes": {"surface": 70},
                    "failed_constraint_ids": ["SUR-02"],
                    "repair_plan": {"retry_recommended": False, "repair_instruction": "Restore pattern."},
                },
            ]),
        ):
            manifests = main.execute_variant(
                run_id, "https://source", "Tea set", "identity", "Quiet luxury",
                "image/png", "source-sha", variant,
            )

        saved = main.RUNS[run_id]["variants"][0]
        self.assertEqual(calls["pipeline"], 2)
        self.assertEqual(saved["status"], "flagged")
        self.assertEqual(len(saved["attempts"]), 2)
        self.assertEqual(saved["attempts"][0]["outcome"], "rejected")
        self.assertIn("base prompt", saved["attempts"][1]["prompt"])
        self.assertEqual(len(manifests), 2)

    def test_malformed_qa_json_preserves_asset_for_review(self):
        qa = main.parse_qa_response("not json")
        self.assertIsNone(qa["score"])
        self.assertIn("preserved", qa["notes"])
        self.assertTrue(qa["critical_drift"])
        self.assertTrue(qa["violations"])

    def test_critical_identity_defect_retries_even_above_score_threshold(self):
        raw = json.dumps({
            "score": 91,
            "critical_drift": True,
            "violations": ["brand badge is missing"],
            "notes": "The product is close, but the required badge is absent.",
        })
        qa = main.parse_qa_response(raw)
        self.assertEqual(qa["score"], 91)
        self.assertTrue(qa["critical_drift"])
        self.assertIn("brand badge is missing", qa["violations"])
        self.assertIn("badge", qa["notes"])
        self.assertFalse(main.identity_passes(qa["score"], 85, qa["critical_drift"]))

    def test_upload_rejects_mismatched_file_signature(self):
        response = TestClient(main.app).post(
            "/api/runs",
            files={"file": ("fake.png", b"not-a-png", "image/png")},
            data={
                "product_name": "Tea set",
                "brief": "Preserve it.",
                "markets": '["Japan"]',
                "channels": '["Editorial"]',
                "environments": '["Studio"]',
                "aesthetic": "Quiet luxury",
            },
        )
        self.assertEqual(response.status_code, 415)

    def test_identity_contract_has_stable_traceable_constraints(self):
        spec = main.normalize_identity_spec(json.dumps({
            "canonical_name": "Moto jacket",
            "product_category": "apparel",
            "constraints": [{
                "id": "LOG-01", "dimension": "logo", "description": "Keep the collar badge",
                "confidence": 0.98, "evidence": "observed", "hard": True,
            }],
        }))
        self.assertEqual(spec["constraints"][0]["id"], "LOG-01")
        self.assertIn("[LOG-01] HARD LOGO", main.format_identity_spec(spec))

    def test_identity_analysis_uses_schema_tool_output_instead_of_text_json(self):
        tool_input = {
            "canonical_name": "BLJ",
            "product_category": "apparel",
            "constraints": [{
                "id": "GEO-01", "dimension": "geometry", "description": "Preserve silhouette",
                "confidence": 0.95, "evidence": "observed", "hard": True,
            }],
        }
        response = SimpleNamespace(content=[
            SimpleNamespace(type="text", text='{"broken": "unterminated'),
            SimpleNamespace(type="tool_use", name="record_identity_contract", input=tool_input),
        ])
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(main, "Anthropic", return_value=client),
        ):
            spec = main.analyze_product_with_claude("https://source", "BLJ", "Preserve shape")
        self.assertEqual(spec["canonical_name"], "BLJ")
        self.assertEqual(spec["constraints"][0]["id"], "GEO-01")

    def test_visual_qa_uses_schema_tool_output_instead_of_text_json(self):
        tool_input = {
            "score": 92,
            "critical_drift": False,
            "axes": {axis: 92 for axis in main.QA_AXES},
            "failed_constraint_ids": [],
            "violations": [],
            "notes": "Identity and channel requirements are preserved.",
            "repair_plan": {
                "severity": "minor",
                "protected_constraints": ["GEO-01"],
                "preserved_creative_elements": ["Rome winter lighting"],
                "repair_instruction": "No repair required.",
                "retry_recommended": False,
            },
        }
        response = SimpleNamespace(content=[
            SimpleNamespace(type="text", text='{"broken": "unterminated'),
            SimpleNamespace(type="tool_use", name="record_visual_qa", input=tool_input),
        ])
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(main, "Anthropic", return_value=client),
            patch.object(main, "b2_image_block", return_value={
                "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
            }),
            patch.object(main, "key_from_durable_url", return_value="output.png"),
        ):
            qa = main.evaluate_variant_with_claude(
                "source.png", "image/png", "https://durable/output.png",
                "[GEO-01] preserve silhouette", "Editorial",
            )
        self.assertEqual(qa["score"], 92)
        self.assertFalse(qa["critical_drift"])
        self.assertEqual(qa["axes"]["geometry"], 92)

    def test_art_direction_is_a_quantified_camera_specification(self):
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="CAMERA: 85 mm; yaw 0 degrees")])

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(main, "Anthropic", return_value=client),
        ):
            prompt = main.direct_variant_with_claude(
                "[GEO-01] Preserve silhouette", "BLJ", "Italy", "Editorial",
                "Winter in Rome", "Quiet luxury",
            )
        self.assertIn("85 mm", prompt)
        self.assertEqual(captured["temperature"], 0.3)
        self.assertIn("CREATIVE DIRECTION", captured["system"])
        self.assertIn("emotionally distinctive campaign image", captured["system"])
        self.assertIn("product bounding box as x/y/w/h percentages", captured["system"])
        self.assertIn("yaw/pitch/roll in degrees", captured["system"])
        self.assertIn("key-to-fill ratio", captured["system"])
        self.assertIn("Artistic adjectives are allowed", captured["system"])

    def test_variant_planner_balances_requested_dimensions(self):
        planned = main.plan_variants(
            ["US", "JP", "FR"], ["Amazon", "Instagram"], ["Studio", "Night"], 6,
        )
        self.assertEqual(len(planned), 6)
        self.assertEqual({item.market for item in planned}, {"US", "JP", "FR"})
        self.assertEqual({item.channel for item in planned}, {"Amazon", "Instagram"})
        self.assertEqual({item.environment for item in planned}, {"Studio", "Night"})

    def test_human_decision_is_persisted_to_campaign_record(self):
        run_id = "decision-run"
        variant = main.Variant("v1", "Studio / Amazon", "US", "Amazon", "Studio", status="ready")
        main.RUNS[run_id] = {
            "run_id": run_id, "status": "complete", "source_key": "source.png",
            "variants": [main.asdict(variant)],
        }
        with patch.object(main, "persist_b2_index"):
            response = TestClient(main.app).post(
                f"/api/runs/{run_id}/variants/v1/decision",
                json={"action": "approve", "note": "Ready for launch"},
            )
        self.assertEqual(response.status_code, 200)
        saved = main.RUNS[run_id]["variants"][0]
        self.assertEqual(saved["approval_status"], "approved")
        self.assertEqual(saved["approval_note"], "Ready for launch")

    def test_interrupted_persisted_run_is_marked_failed(self):
        path = self.runtime / "interrupted.json"
        path.write_text(
            json.dumps(
                {
                    "run_id": "interrupted",
                    "status": "running",
                    "variants": [{"id": "v1", "status": "generating"}],
                }
            ),
            encoding="utf-8",
        )
        loaded = main.load_runs()
        self.assertEqual(loaded["interrupted"]["status"], "failed")
        self.assertEqual(loaded["interrupted"]["variants"][0]["status"], "failed")

    def test_campaign_export_contains_durable_lineage(self):
        main.RUNS["export-run"] = {
            "run_id": "export-run",
            "product_name": "Tea set",
            "status": "complete",
            "variants": [{
                "id": "v1",
                "market": "Japan",
                "channel": "Editorial",
                "environment": "Studio",
                "status": "ready",
                "score": 92,
                "qa_notes": "Identity preserved.",
                "sha256": "asset-sha",
                "durable_url": "https://b2.example/bucket/asset.png",
                "provider": "GMI Cloud / Seedream 5",
            }],
        }
        response = TestClient(main.app).get("/api/runs/export-run/export?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("asset-sha", response.text)
        self.assertIn("https://b2.example/bucket/asset.png", response.text)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_local_run_archive_summarizes_flagged_and_ready(self):
        main.RUNS["summary-run"] = {
            "run_id": "summary-run",
            "product_name": "Tea set",
            "status": "complete",
            "created_at": "2026-07-24T00:00:00+00:00",
            "variants": [{"status": "ready"}, {"status": "flagged"}],
        }
        response = TestClient(main.app).get("/api/runs")
        self.assertEqual(response.status_code, 200)
        summary = response.json()[0]
        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["flagged_count"], 1)


if __name__ == "__main__":
    unittest.main()
