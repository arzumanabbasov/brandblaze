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
        main.RUNS[run_id] = {"run_id": run_id, "status": "running", "variants": [main.asdict(variant)]}
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
            patch.object(main, "evaluate_variant_with_claude", return_value=(96, "Identity preserved.")),
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
        main.RUNS[run_id] = {"run_id": run_id, "status": "running", "variants": [main.asdict(variant)]}
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
            patch.object(main, "evaluate_variant_with_claude", side_effect=[(40, "Handle changed."), (70, "Pattern drift.")]),
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
        self.assertIn("Handle changed", saved["attempts"][1]["prompt"])
        self.assertEqual(len(manifests), 2)

    def test_malformed_qa_json_preserves_asset_for_review(self):
        score, notes = main.parse_qa_response("not json")
        self.assertIsNone(score)
        self.assertIn("preserved", notes)

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
