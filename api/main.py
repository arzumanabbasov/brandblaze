from __future__ import annotations

import json
import hashlib
import base64
import csv
import io
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import boto3
from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from genblaze_core import Asset, KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

app = FastAPI(title="BrandBlaze Genblaze API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass
class Variant:
    id: str
    label: str
    market: str
    channel: str
    environment: str
    objective: str | None = None
    status: str = "queued"
    url: str | None = None
    durable_url: str | None = None
    provider: str | None = None
    score: int | None = None
    qa_notes: str | None = None
    critical_drift: bool = False
    qa_violations: list[str] | None = None
    qa_axes: dict[str, int] | None = None
    failed_constraint_ids: list[str] | None = None
    repair_plan: dict[str, Any] | None = None
    approval_status: str = "pending"
    approval_note: str | None = None
    sha256: str | None = None
    attempts: list[dict[str, Any]] | None = None


RUNTIME_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "runs"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def load_runs() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for path in RUNTIME_DIR.glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            if run.get("status") in {"queued", "running"}:
                run["status"] = "failed"
                run["error"] = "Run was interrupted by a backend restart. Start a new run."
                for variant in run.get("variants", []):
                    if variant.get("status") in {"queued", "generating"}:
                        variant["status"] = "failed"
            loaded[run["run_id"]] = run
        except (OSError, ValueError, KeyError):
            continue
    return loaded


RUNS: dict[str, dict[str, Any]] = load_runs()
RUN_LOCK = threading.Lock()

CHANNEL_REQUIREMENTS = {
    "Amazon": "Marketplace hero: full product visible, clean composition, minimal props, generous safe margins, no text, dominant product recognition.",
    "E-commerce PDP": "PDP-ready: full product visible, accurate scale and color, clean hierarchy, minimal props, consistent safe margins.",
    "Instagram": "Mobile-first awareness image: immediate focal hierarchy, strong first-frame impact, crop-safe center, intentional negative space.",
    "TikTok": "Vertical mobile composition: immediate product recognition, energetic depth, crop-safe center, uncluttered silhouette.",
    "Billboard": "Long-distance readability: simple silhouette, extreme product recognition, minimal clutter, bold tonal separation.",
    "Email": "Campaign email composition: headline-safe negative space, landscape-friendly hierarchy, CTA-safe region, clear product focal point.",
    "Editorial": "Authored editorial composition with atmospheric context while preserving unmistakable commercial product recognition.",
    "Retail display": "Retail display composition: strong recognition from distance, environmental context, no product obstruction.",
    "Pinterest": "Save-worthy vertical composition with clear product hierarchy, tactile detail, and crop-safe negative space.",
    "Print campaign": "High-resolution print composition with deliberate negative space, controlled detail, and premium product prominence.",
}

CHANNEL_OBJECTIVES = {
    "Amazon": "Conversion-ready marketplace hero",
    "E-commerce PDP": "Product-detail page conversion",
    "Instagram": "Mobile awareness and engagement",
    "TikTok": "Scroll-stopping mobile awareness",
    "Billboard": "Long-distance brand recognition",
    "Email": "Launch message with CTA-safe space",
    "Editorial": "Brand storytelling and desirability",
    "Retail display": "In-store recognition and context",
    "Pinterest": "Discovery and visual consideration",
    "Print campaign": "Premium campaign storytelling",
}


def persist_run(run_id: str) -> None:
    path = RUNTIME_DIR / f"{run_id}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(RUNS[run_id], indent=2), encoding="utf-8")
    temp.replace(path)


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detected_media_type(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    variants = run.get("variants", [])
    return {
        "run_id": run.get("run_id"),
        "product_name": run.get("product_name"),
        "status": run.get("status"),
        "aesthetic": run.get("aesthetic"),
        "created_at": run.get("created_at"),
        "completed_at": run.get("completed_at"),
        "variant_count": len(variants),
        "ready_count": sum(item.get("status") == "ready" for item in variants),
        "flagged_count": sum(item.get("status") == "flagged" for item in variants),
        "approved_count": sum(item.get("approval_status") == "approved" for item in variants),
    }


def plan_variants(
    markets: list[str],
    channels: list[str],
    environments: list[str],
    limit: int,
) -> list[Variant]:
    candidates = [
        (market, channel, environment)
        for market in markets for channel in channels for environment in environments
    ]
    chosen: list[tuple[str, str, str]] = []
    while candidates and len(chosen) < limit:
        def coverage_score(item: tuple[str, str, str]) -> tuple[int, int, int, int]:
            market, channel, environment = item
            seen_m = sum(existing[0] == market for existing in chosen)
            seen_c = sum(existing[1] == channel for existing in chosen)
            seen_e = sum(existing[2] == environment for existing in chosen)
            seen_pairs = sum(
                (existing[0], existing[1]) == (market, channel)
                or (existing[0], existing[2]) == (market, environment)
                or (existing[1], existing[2]) == (channel, environment)
                for existing in chosen
            )
            return (-seen_pairs, -seen_m, -seen_c, -seen_e)
        best = max(candidates, key=coverage_score)
        chosen.append(best)
        candidates.remove(best)
    return [
        Variant(
            id=uuid.uuid4().hex[:10],
            label=f"{environment} / {channel}",
            market=market,
            channel=channel,
            environment=environment,
            objective=CHANNEL_OBJECTIVES.get(channel, "Commercial product communication"),
        )
        for market, channel, environment in chosen
    ]


def b2_client():
    region = required("B2_REGION")
    return boto3.client(
        "s3",
        endpoint_url=f"https://s3.{region}.backblazeb2.com",
        aws_access_key_id=required("B2_KEY_ID"),
        aws_secret_access_key=required("B2_APP_KEY"),
        region_name=region,
    )


def storage_sink() -> ObjectStorageSink:
    return ObjectStorageSink(
        S3StorageBackend.for_backblaze(
            required("B2_BUCKET"),
            region=required("B2_REGION"),
            public_url_base=os.getenv("B2_PUBLIC_BASE_URL") or None,
        ),
        key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
        prefix="brandblaze",
    )


def source_b2_url(client, key: str) -> str:
    custom = os.getenv("B2_PUBLIC_BASE_URL", "").rstrip("/")
    if custom:
        return f"{custom}/{key}"
    # GMI Cloud and Claude need temporary read access to private source images.
    # Outputs remain durable in B2; only this generation-time URL expires.
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": required("B2_BUCKET"), "Key": key},
        ExpiresIn=int(os.getenv("B2_SOURCE_URL_TTL", "3600")),
    )


def key_from_durable_url(url: str) -> str:
    path = unquote(urlparse(url).path).lstrip("/")
    bucket_prefix = f"{required('B2_BUCKET')}/"
    if bucket_prefix not in path:
        raise RuntimeError("Generated B2 asset URL does not contain the configured bucket.")
    return path.split(bucket_prefix, 1)[1]


def presign_output_url(durable_url: str) -> str:
    return b2_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": required("B2_BUCKET"), "Key": key_from_durable_url(durable_url)},
        ExpiresIn=int(os.getenv("B2_OUTPUT_URL_TTL", "3600")),
    )


def b2_image_block(key: str, media_type: str | None = None) -> dict[str, Any]:
    response = b2_client().get_object(Bucket=required("B2_BUCKET"), Key=key)
    payload = response["Body"].read()
    detected = detected_media_type(payload)
    resolved_type = detected or media_type
    if resolved_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise RuntimeError("B2 object is not a supported product image.")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": resolved_type,
            "data": base64.b64encode(payload).decode("ascii"),
        },
    }


def present_run(run: dict[str, Any]) -> dict[str, Any]:
    presented = json.loads(json.dumps(run))
    try:
        if presented.get("source_key"):
            presented["source_url"] = source_b2_url(b2_client(), presented["source_key"])
        for variant in presented.get("variants", []):
            if variant.get("durable_url"):
                variant["url"] = presign_output_url(variant["durable_url"])
            for attempt in variant.get("attempts") or []:
                if attempt.get("durable_url"):
                    attempt["url"] = presign_output_url(attempt["durable_url"])
    except Exception:
        # Durable URLs and metadata remain useful even if a temporary URL cannot be refreshed.
        pass
    return presented


def load_b2_run(run_id: str) -> dict[str, Any] | None:
    try:
        response = b2_client().get_object(
            Bucket=required("B2_BUCKET"),
            Key=f"brandblaze/indexes/{run_id}.json",
        )
        return json.loads(response["Body"].read())
    except Exception:
        return None


def list_b2_runs(limit: int) -> list[dict[str, Any]]:
    client = b2_client()
    response = client.list_objects_v2(
        Bucket=required("B2_BUCKET"),
        Prefix="brandblaze/indexes/",
        MaxKeys=min(limit, 100),
    )
    summaries: list[dict[str, Any]] = []
    for item in sorted(response.get("Contents", []), key=lambda value: value.get("LastModified"), reverse=True):
        run_id = Path(item["Key"]).stem
        run = load_b2_run(run_id)
        if run:
            summaries.append(run_summary(run))
    return summaries


def persist_b2_index(run_id: str) -> None:
    index = json.loads(json.dumps(RUNS[run_id]))
    index["source_url"] = f"b2://{required('B2_BUCKET')}/{index['source_key']}"
    for variant in index["variants"]:
        variant["url"] = variant.get("durable_url")
        for attempt in variant.get("attempts") or []:
            attempt["url"] = attempt.get("durable_url")
    b2_client().put_object(
        Bucket=required("B2_BUCKET"),
        Key=f"brandblaze/indexes/{run_id}.json",
        Body=json.dumps(index, indent=2).encode(),
        ContentType="application/json",
    )


def provider_for():
    from genblaze_gmicloud import GMICloudImageProvider

    required("GMI_API_KEY")
    return GMICloudImageProvider(), os.getenv("GMI_IMAGE_MODEL", "seedream-5.0-lite"), "GMI Cloud / Seedream 5"


def claude_text(message) -> str:
    return "\n".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()


def parse_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def normalize_identity_spec(raw: str) -> dict[str, Any]:
    value = parse_json_object(raw)
    constraints: list[dict[str, Any]] = []
    for index, item in enumerate(value.get("constraints") or [], 1):
        if not isinstance(item, dict) or not str(item.get("description") or "").strip():
            continue
        dimension = str(item.get("dimension") or "identity").lower().replace(" ", "_")
        prefix = "".join(part[0] for part in dimension.split("_") if part)[:3].upper() or "ID"
        constraints.append({
            "id": str(item.get("id") or f"{prefix}-{index:02d}")[:24],
            "dimension": dimension[:40],
            "description": str(item["description"]).strip()[:500],
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.7)))),
            "evidence": str(item.get("evidence") or "observed")[:40],
            "hard": bool(item.get("hard", True)),
        })
    if not constraints:
        raise ValueError("Claude returned no usable identity constraints.")
    return {
        "version": 1,
        "canonical_name": str(value.get("canonical_name") or "Unnamed product")[:160],
        "product_category": str(value.get("product_category") or "product")[:100],
        "constraints": constraints,
        "camera_evidence": value.get("camera_evidence") if isinstance(value.get("camera_evidence"), dict) else {},
        "unknown_or_ambiguous_details": [
            str(item)[:240] for item in (value.get("unknown_or_ambiguous_details") or [])
            if isinstance(item, str)
        ][:12],
    }


def format_identity_spec(spec: dict[str, Any]) -> str:
    lines = [f"{spec.get('canonical_name', 'Product')} · {spec.get('product_category', 'product')}"]
    for item in spec.get("constraints", []):
        confidence = round(float(item.get("confidence", 0)) * 100)
        strength = "HARD" if item.get("hard") else "SOFT"
        lines.append(
            f"[{item.get('id')}] {strength} {str(item.get('dimension', 'identity')).upper()} "
            f"({confidence}% confidence): {item.get('description')}"
        )
    unknown = spec.get("unknown_or_ambiguous_details") or []
    if unknown:
        lines.append("DO NOT INVENT: " + "; ".join(unknown))
    return "\n".join(lines)


def analyze_product_with_claude(source_url: str, product_name: str, user_brief: str) -> dict[str, Any]:
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=2200,
        temperature=0.2,
        system=(
            "You are the visual continuity director for a world-class product photography studio. "
            "Inspect the reference with forensic care and produce a machine-readable product contract. "
            "Separate immutable identity from mutable art direction. Never invent occluded details. "
            "Call the provided tool exactly once. Use no more than 16 concise constraints. Use stable "
            "unique constraint IDs. Treat visible or owner-specified logos, text, component count, "
            "geometry, color, material, and distinctive hardware as hard constraints."
        ),
        tools=[{
            "name": "record_identity_contract",
            "description": "Record the canonical, machine-readable product identity contract.",
            "input_schema": {
                "type": "object",
                "required": ["canonical_name", "product_category", "constraints"],
                "properties": {
                    "canonical_name": {"type": "string"},
                    "product_category": {"type": "string"},
                    "constraints": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {
                            "type": "object",
                            "required": ["id", "dimension", "description", "confidence", "evidence", "hard"],
                            "properties": {
                                "id": {"type": "string"},
                                "dimension": {
                                    "type": "string",
                                    "enum": ["geometry", "component_count", "color", "material", "logo", "text", "hardware", "surface"],
                                },
                                "description": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "evidence": {"type": "string", "enum": ["observed", "owner_provided", "inferred"]},
                                "hard": {"type": "boolean"},
                            },
                        },
                    },
                    "camera_evidence": {
                        "type": "object",
                        "properties": {
                            "visible_faces": {"type": "array", "items": {"type": "string"}},
                            "occluded_details": {"type": "array", "items": {"type": "string"}},
                            "view_limitations": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "unknown_or_ambiguous_details": {"type": "array", "items": {"type": "string"}},
                },
            },
        }],
        tool_choice={"type": "tool", "name": "record_identity_contract"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "url", "url": source_url}},
                {
                    "type": "text",
                    "text": f"Product: {product_name}\nOwner notes: {user_brief}\nCreate the identity lock.",
                },
            ],
        }],
    )
    tool_input = next(
        (
            block.input for block in response.content
            if getattr(block, "type", "") == "tool_use"
            and getattr(block, "name", "") == "record_identity_contract"
        ),
        None,
    )
    if not isinstance(tool_input, dict):
        raise RuntimeError("Claude returned no structured identity contract.")
    return normalize_identity_spec(json.dumps(tool_input))


def direct_variant_with_claude(
    identity_map: str,
    product_name: str,
    market: str,
    channel: str,
    environment: str,
    aesthetic: str,
    repair_plan: dict[str, Any] | None = None,
) -> str:
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=1100,
        temperature=0.35,
        system=(
            "You are an elite commercial art director writing executable prompts for a high-end "
            "image-editing model. Create one decisive photographic concept, not options. The reference "
            "product must remain geometrically and materially faithful. Translate market context through "
            "light, space, color, props, and composition—never stereotypes, flags, tourist symbols, or "
            "written text. Specify lens, camera height, framing, lighting, palette, surface, depth, "
            "atmosphere, and product placement. Keep the complete product unobstructed, uncropped, "
            "tack-sharp, and visually dominant at 65-80% of frame height. Preserve every named logo, "
            "label, closure, pocket, hardware element, seam, and distinctive component exactly. "
            "Make it authored, tactile, expensive, and editorial—not "
            "generic AI art. Output only the final 180–260 word image prompt. Put identity constraints "
            "first and negative constraints last."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"PRODUCT: {product_name}\nMARKET: {market}\nCHANNEL: {channel}\n"
                f"ENVIRONMENT: {environment}\nAESTHETIC: {aesthetic}\n"
                f"CAMPAIGN OBJECTIVE: {CHANNEL_OBJECTIVES.get(channel, 'Clear commercial product communication')}\n"
                f"CHANNEL REQUIREMENTS: {CHANNEL_REQUIREMENTS.get(channel, 'Keep the product dominant, legible, and crop-safe.')}\n\n"
                f"CANONICAL IDENTITY LOCK:\n{identity_map}"
                + (
                    "\n\nTARGETED REPAIR CONTRACT:\n"
                    f"{json.dumps(repair_plan, indent=2)}\n"
                    "Fix only failed constraints and preserve the successful creative elements."
                    if repair_plan else ""
                )
            ),
        }],
    )
    prompt = claude_text(response)
    if not prompt:
        raise RuntimeError("Claude returned an empty art-direction prompt.")
    return prompt


QA_AXES = (
    "geometry", "component_count", "color", "material", "logo_markings",
    "text_integrity", "product_prominence", "channel_fit",
)


def normalize_qa_result(result: dict[str, Any]) -> dict[str, Any]:
    try:
        score = max(0, min(100, int(result["score"])))
        axes = {
            axis: max(0, min(100, int((result.get("axes") or {}).get(axis, score))))
            for axis in QA_AXES
        }
        violations = [
            str(item)[:180]
            for item in result.get("violations", [])
            if isinstance(item, str) and item.strip()
        ][:8]
        failed_ids = [
            str(item)[:24] for item in result.get("failed_constraint_ids", [])
            if isinstance(item, str) and item.strip()
        ][:12]
        repair = result.get("repair_plan") if isinstance(result.get("repair_plan"), dict) else {}
        return {
            "score": score,
            "notes": str(result.get("notes") or "No QA notes returned.")[:500],
            "critical_drift": bool(result.get("critical_drift", False)),
            "violations": violations,
            "axes": axes,
            "failed_constraint_ids": failed_ids,
            "repair_plan": {
                "severity": str(repair.get("severity") or ("critical" if result.get("critical_drift") else "moderate"))[:20],
                "protected_constraints": [str(item)[:24] for item in repair.get("protected_constraints", []) if isinstance(item, str)][:12],
                "preserved_creative_elements": [str(item)[:160] for item in repair.get("preserved_creative_elements", []) if isinstance(item, str)][:8],
                "repair_instruction": str(repair.get("repair_instruction") or result.get("notes") or "")[:600],
                "retry_recommended": bool(repair.get("retry_recommended", True)),
            },
        }
    except (KeyError, TypeError, ValueError):
        return {
            "score": None,
            "notes": "Claude QA returned an unreadable score; the generated asset was preserved for review.",
            "critical_drift": True,
            "violations": ["QA result was unreadable"],
            "axes": {},
            "failed_constraint_ids": [],
            "repair_plan": {"severity": "unknown", "retry_recommended": False, "repair_instruction": "Human review required."},
        }


def parse_qa_response(raw: str) -> dict[str, Any]:
    try:
        return normalize_qa_result(parse_json_object(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return normalize_qa_result({})


def identity_passes(score: int | None, threshold: int, critical_drift: bool) -> bool:
    return score is not None and score >= threshold and not critical_drift


def evaluate_variant_with_claude(
    source_key: str,
    source_media_type: str,
    output_durable_url: str,
    identity_map: str,
    channel: str,
) -> dict[str, Any]:
    source_image = b2_image_block(source_key, source_media_type)
    output_image = b2_image_block(key_from_durable_url(output_durable_url))
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=1400,
        temperature=0,
        system=(
            "You are a strict product-continuity inspector. Compare the source product in image one "
            "with the generated product in image two, using the canonical identity lock as the binding "
            "specification. Ignore background, lighting, camera angle, and props. Inspect geometry, color, "
            "material, surface, pattern, logo/label fidelity, hardware, component count, and distinctive "
            "construction. Set critical_drift=true when any named logo or marking is missing or altered, "
            "the primary color or material changes, a distinctive component is missing or added, or core "
            "geometry changes. A critical defect cannot be compensated for by overall visual similarity. "
            "Call the provided QA tool exactly once. Never print the result as prose or JSON text."
        ),
        tools=[{
            "name": "record_visual_qa",
            "description": "Record constraint-level product continuity and channel QA.",
            "input_schema": {
                "type": "object",
                "required": ["score", "critical_drift", "axes", "failed_constraint_ids", "violations", "notes", "repair_plan"],
                "properties": {
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "critical_drift": {"type": "boolean"},
                    "axes": {
                        "type": "object",
                        "required": list(QA_AXES),
                        "properties": {axis: {"type": "integer", "minimum": 0, "maximum": 100} for axis in QA_AXES},
                    },
                    "failed_constraint_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                    "violations": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "notes": {"type": "string"},
                    "repair_plan": {
                        "type": "object",
                        "required": ["severity", "protected_constraints", "preserved_creative_elements", "repair_instruction", "retry_recommended"],
                        "properties": {
                            "severity": {"type": "string", "enum": ["minor", "moderate", "critical"]},
                            "protected_constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                            "preserved_creative_elements": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                            "repair_instruction": {"type": "string"},
                            "retry_recommended": {"type": "boolean"},
                        },
                    },
                },
            },
        }],
        tool_choice={"type": "tool", "name": "record_visual_qa"},
        messages=[{
            "role": "user",
            "content": [
                source_image,
                output_image,
                {
                    "type": "text",
                    "text": (
                        f"Evaluate identity and channel fitness.\n\nCANONICAL IDENTITY LOCK:\n{identity_map}\n\n"
                        f"CHANNEL: {channel}\nREQUIREMENTS: "
                        f"{CHANNEL_REQUIREMENTS.get(channel, 'Product must be dominant and crop-safe.')}"
                    ),
                },
            ],
        }],
    )
    tool_input = next(
        (
            block.input for block in response.content
            if getattr(block, "type", "") == "tool_use"
            and getattr(block, "name", "") == "record_visual_qa"
        ),
        None,
    )
    if not isinstance(tool_input, dict):
        raise RuntimeError("Claude returned no structured visual QA record.")
    return normalize_qa_result(tool_input)


def update_variant(run_id: str, variant_id: str, **changes: Any) -> None:
    with RUN_LOCK:
        for variant in RUNS[run_id]["variants"]:
            if variant["id"] == variant_id:
                variant.update(changes)
                persist_run(run_id)
                break


def append_attempt(run_id: str, variant_id: str, attempt: dict[str, Any]) -> None:
    with RUN_LOCK:
        for variant in RUNS[run_id]["variants"]:
            if variant["id"] == variant_id:
                variant.setdefault("attempts", []).append(attempt)
                persist_run(run_id)
                break


def execute_variant(
    run_id: str,
    source_url: str,
    product_name: str,
    identity_map: str,
    aesthetic: str,
    source_media_type: str,
    source_sha256: str,
    variant: Variant,
    preserve_attempts: bool = False,
):
    existing_attempts = next(
        (item.get("attempts") or [] for item in RUNS[run_id]["variants"] if item["id"] == variant.id),
        [],
    )
    update_variant(
        run_id,
        variant.id,
        status="generating",
        approval_status="pending",
        approval_note=None,
        attempts=existing_attempts if preserve_attempts else [],
    )
    provider, model, provider_label = provider_for()
    base_prompt = direct_variant_with_claude(
        identity_map,
        product_name,
        variant.market,
        variant.channel,
        variant.environment,
        aesthetic,
    )
    threshold = int(os.getenv("IDENTITY_QA_THRESHOLD", "85"))
    max_attempts = max(1, int(os.getenv("IDENTITY_MAX_ATTEMPTS", "2")))
    manifests: list[str] = []
    repair_plan: dict[str, Any] | None = None
    attempt_offset = len(existing_attempts) if preserve_attempts else 0

    for local_attempt in range(1, max_attempts + 1):
        attempt_number = attempt_offset + local_attempt
        prompt = base_prompt
        if repair_plan:
            prompt = direct_variant_with_claude(
                identity_map,
                product_name,
                variant.market,
                variant.channel,
                variant.environment,
                aesthetic,
                repair_plan,
            )
        step_params: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "modality": Modality.IMAGE,
            "external_inputs": [
                Asset(url=source_url, media_type=source_media_type, sha256=source_sha256)
            ],
            "fallback_models": [item.strip() for item in os.getenv("GENBLAZE_FALLBACK_MODELS", "").split(",") if item.strip()],
            "size": os.getenv("GMI_IMAGE_SIZE", "2048x2048"),
            "output_format": os.getenv("GMI_OUTPUT_FORMAT", "png"),
        }
        result = (
            Pipeline(f"brandblaze-variant-attempt-{attempt_number}", tenant_id=run_id)
            .step(provider, **step_params)
            .run(sink=storage_sink(), timeout=int(os.getenv("GENBLAZE_TIMEOUT", "300")))
        )
        if not result.run.steps or not result.run.steps[-1].assets:
            raise RuntimeError("GMI returned no image asset.")
        asset = result.run.steps[-1].assets[0]
        if not asset.url or not asset.sha256:
            raise RuntimeError("Generated asset is missing its B2 URL or SHA-256 digest.")
        if not result.manifest.verify():
            raise RuntimeError("Genblaze manifest verification failed.")

        manifest_url = str(result.manifest.manifest_uri or "")
        if manifest_url:
            manifests.append(manifest_url)
        display_url = presign_output_url(asset.url)
        try:
            qa = evaluate_variant_with_claude(
                RUNS[run_id]["source_key"],
                source_media_type,
                asset.url,
                identity_map,
                variant.channel,
            )
        except Exception as exc:
            qa = {
                "score": None, "notes": f"Claude QA was unavailable: {exc}",
                "critical_drift": True, "violations": ["QA service was unavailable"],
                "axes": {}, "failed_constraint_ids": [],
                "repair_plan": {"retry_recommended": False, "repair_instruction": "Human review required."},
            }
        score = qa["score"]
        qa_notes = qa["notes"]
        critical_drift = qa["critical_drift"]
        qa_violations = qa["violations"]
        accepted = identity_passes(score, threshold, critical_drift)
        append_attempt(run_id, variant.id, {
            "attempt": attempt_number,
            "url": display_url,
            "durable_url": asset.url,
            "sha256": asset.sha256,
            "manifest_url": manifest_url,
            "prompt": prompt,
            "score": score,
            "qa_notes": qa_notes,
            "critical_drift": critical_drift,
            "qa_violations": qa_violations,
            "qa_axes": qa["axes"],
            "failed_constraint_ids": qa["failed_constraint_ids"],
            "repair_plan": qa["repair_plan"],
            "outcome": "accepted" if accepted else "rejected",
        })
        update_variant(
            run_id,
            variant.id,
            status="ready" if accepted else "reviewing",
            url=display_url,
            durable_url=asset.url,
            provider=provider_label,
            score=score,
            qa_notes=qa_notes,
            critical_drift=critical_drift,
            qa_violations=qa_violations,
            qa_axes=qa["axes"],
            failed_constraint_ids=qa["failed_constraint_ids"],
            repair_plan=qa["repair_plan"],
            sha256=asset.sha256,
        )
        if accepted:
            return manifests
        repair_plan = qa["repair_plan"]
        if not repair_plan.get("retry_recommended", True):
            break

    update_variant(run_id, variant.id, status="flagged")
    return manifests


def process_run(
    run_id: str,
    source_url: str,
    product_name: str,
    brief: str,
    aesthetic: str,
    source_media_type: str,
    source_sha256: str,
    variants: list[Variant],
):
    with RUN_LOCK:
        RUNS[run_id]["status"] = "running"
        persist_run(run_id)

    manifests: list[str] = []
    failures: list[str] = []
    try:
        identity_spec = analyze_product_with_claude(source_url, product_name, brief)
        identity_map = format_identity_spec(identity_spec)
        with RUN_LOCK:
            RUNS[run_id]["identity_spec"] = identity_spec
            RUNS[run_id]["identity_map"] = identity_map
            RUNS[run_id]["creative_director"] = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
            RUNS[run_id]["image_provider"] = f"GMI Cloud / {os.getenv('GMI_IMAGE_MODEL', 'seedream-5.0-lite')}"
            persist_run(run_id)
    except Exception as exc:
        with RUN_LOCK:
            RUNS[run_id]["status"] = "failed"
            RUNS[run_id]["error"] = f"Claude identity analysis failed: {exc}"
            for variant in RUNS[run_id]["variants"]:
                variant["status"] = "failed"
            persist_run(run_id)
        return

    max_workers = min(int(os.getenv("GENBLAZE_CONCURRENCY", "3")), len(variants))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                execute_variant,
                run_id,
                source_url,
                product_name,
                identity_map,
                aesthetic,
                source_media_type,
                source_sha256,
                variant,
            ): variant
            for variant in variants
        }
        for future in as_completed(future_map):
            variant = future_map[future]
            try:
                manifests.extend(future.result())
            except Exception as exc:
                failures.append(f"{variant.label}: {exc}")
                update_variant(run_id, variant.id, status="failed")

    with RUN_LOCK:
        RUNS[run_id]["manifest_urls"] = manifests
        RUNS[run_id]["status"] = "complete" if manifests else "failed"
        RUNS[run_id]["completed_at"] = utc_now()
        if failures:
            RUNS[run_id]["error"] = " | ".join(failures)
        persist_run(run_id)

    # Persist the application-level index beside Genblaze's per-run manifests.
    try:
        persist_b2_index(run_id)
    except Exception as exc:
        with RUN_LOCK:
            existing = RUNS[run_id].get("error")
            RUNS[run_id]["error"] = f"{existing + ' | ' if existing else ''}B2 run-index write failed: {exc}"
            persist_run(run_id)


@app.get("/health")
def health():
    return {
        "service": "brandblaze",
        "genblaze": True,
        "backblaze_b2": bool(os.getenv("B2_BUCKET")),
        "creative_director": "claude" if os.getenv("ANTHROPIC_API_KEY") else None,
        "image_provider": "gmicloud" if os.getenv("GMI_API_KEY") else None,
        "qa_threshold": int(os.getenv("IDENTITY_QA_THRESHOLD", "85")),
        "max_attempts": max(1, int(os.getenv("IDENTITY_MAX_ATTEMPTS", "2"))),
    }


class VariantDecision(BaseModel):
    action: Literal["approve", "reject"]
    note: str = ""


@app.get("/api/runs")
def list_runs(
    include_b2: bool = Query(False),
    limit: int = Query(25, ge=1, le=100),
):
    combined = {run_id: run_summary(run) for run_id, run in RUNS.items()}
    if include_b2:
        try:
            for summary in list_b2_runs(limit):
                combined.setdefault(summary["run_id"], summary)
        except Exception as exc:
            raise HTTPException(503, f"B2 archive read failed: {exc}") from exc
    return sorted(
        combined.values(),
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )[:limit]


@app.post("/api/runs", status_code=202)
async def create_run(
    file: UploadFile = File(...),
    product_name: str = Form(...),
    brief: str = Form(...),
    markets: str = Form(...),
    channels: str = Form(...),
    environments: str = Form(...),
    aesthetic: str = Form(...),
):
    product_name = product_name.strip()
    brief = brief.strip()
    if not product_name or not brief:
        raise HTTPException(422, "Product name and identity lock are required.")
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Use a PNG, JPG, or WEBP product image.")
    payload = await file.read()
    actual_media_type = detected_media_type(payload)
    if actual_media_type != file.content_type:
        raise HTTPException(415, "The image bytes do not match the declared PNG, JPG, or WEBP type.")
    if len(payload) > 20 * 1024 * 1024:
        raise HTTPException(413, "Product image exceeds 20 MB.")

    try:
        parsed_markets = json.loads(markets)
        parsed_channels = json.loads(channels)
        parsed_environments = json.loads(environments)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "Markets, channels, and environments must be JSON arrays.") from exc
    selections = (parsed_markets, parsed_channels, parsed_environments)
    if any(not isinstance(items, list) or not items for items in selections):
        raise HTTPException(422, "Select at least one market, channel, and environment.")
    if any(not isinstance(item, str) or not item.strip() for items in selections for item in items):
        raise HTTPException(422, "Every market, channel, and environment must be non-empty text.")

    run_id = uuid.uuid4().hex[:12]
    source_sha256 = hashlib.sha256(payload).hexdigest()
    extension = (Path(file.filename or "product.jpg").suffix or ".jpg").lower()
    source_key = f"brandblaze/sources/{run_id}/original{extension}"
    try:
        client = b2_client()
        client.put_object(
            Bucket=required("B2_BUCKET"),
            Key=source_key,
            Body=payload,
            ContentType=file.content_type,
            Metadata={"run-id": run_id, "product-name": product_name[:256]},
        )
        source_url = source_b2_url(client, source_key)
    except Exception as exc:
        raise HTTPException(503, f"Backblaze B2 ingest failed: {exc}") from exc

    variants = plan_variants(
        parsed_markets,
        parsed_channels,
        parsed_environments,
        int(os.getenv("MAX_VARIANTS_PER_RUN", "12")),
    )

    RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "source_url": source_url,
        "source_key": source_key,
        "product_name": product_name,
        "aesthetic": aesthetic,
        "source_sha256": source_sha256,
        "source_media_type": actual_media_type,
        "created_at": utc_now(),
        "qa_threshold": int(os.getenv("IDENTITY_QA_THRESHOLD", "85")),
        "max_attempts": max(1, int(os.getenv("IDENTITY_MAX_ATTEMPTS", "2"))),
        "variants": [asdict(item) for item in variants],
        "manifest_urls": [],
    }
    persist_run(run_id)
    threading.Thread(
        target=process_run,
        args=(
            run_id,
            source_url,
            product_name,
            brief,
            aesthetic,
            file.content_type,
            source_sha256,
            variants,
        ),
        daemon=True,
    ).start()
    return RUNS[run_id]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    if run_id not in RUNS:
        archived = load_b2_run(run_id)
        if not archived:
            raise HTTPException(404, "Run not found.")
        RUNS[run_id] = archived
        persist_run(run_id)
    return present_run(RUNS[run_id])


@app.post("/api/runs/{run_id}/variants/{variant_id}/decision")
def decide_variant(run_id: str, variant_id: str, decision: VariantDecision):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    variant = next((item for item in run["variants"] if item["id"] == variant_id), None)
    if not variant:
        raise HTTPException(404, "Variant not found.")
    if variant.get("status") not in {"ready", "flagged"}:
        raise HTTPException(409, "Only completed variants can receive a decision.")
    update_variant(
        run_id,
        variant_id,
        approval_status="approved" if decision.action == "approve" else "rejected",
        approval_note=decision.note.strip()[:500] or None,
        decided_at=utc_now(),
    )
    try:
        persist_b2_index(run_id)
    except Exception as exc:
        raise HTTPException(503, f"Decision saved locally but B2 archive update failed: {exc}") from exc
    return present_run(RUNS[run_id])


def regenerate_one(run_id: str, variant: Variant) -> None:
    run = RUNS[run_id]
    try:
        manifests = execute_variant(
            run_id,
            source_b2_url(b2_client(), run["source_key"]),
            run["product_name"],
            run["identity_map"],
            run["aesthetic"],
            run["source_media_type"],
            run["source_sha256"],
            variant,
            preserve_attempts=True,
        )
        with RUN_LOCK:
            RUNS[run_id]["manifest_urls"] = list(dict.fromkeys((RUNS[run_id].get("manifest_urls") or []) + manifests))
            RUNS[run_id]["status"] = "complete"
            RUNS[run_id]["completed_at"] = utc_now()
            persist_run(run_id)
        persist_b2_index(run_id)
    except Exception as exc:
        update_variant(run_id, variant.id, status="failed", qa_notes=f"Regeneration failed: {exc}")
        with RUN_LOCK:
            RUNS[run_id]["status"] = "complete"
            RUNS[run_id]["error"] = f"Single-asset regeneration failed: {exc}"
            persist_run(run_id)


@app.post("/api/runs/{run_id}/variants/{variant_id}/regenerate", status_code=202)
def regenerate_variant(run_id: str, variant_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    value = next((item for item in run["variants"] if item["id"] == variant_id), None)
    if not value:
        raise HTTPException(404, "Variant not found.")
    if value.get("status") in {"queued", "generating", "reviewing"}:
        raise HTTPException(409, "Variant is already in progress.")
    allowed = {field.name for field in Variant.__dataclass_fields__.values()}
    variant = Variant(**{key: val for key, val in value.items() if key in allowed})
    with RUN_LOCK:
        RUNS[run_id]["status"] = "running"
        persist_run(run_id)
    update_variant(run_id, variant_id, status="queued", approval_status="pending", approval_note=None)
    threading.Thread(target=regenerate_one, args=(run_id, variant), daemon=True).start()
    return present_run(RUNS[run_id])


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str, format: str = Query("json", pattern="^(json|csv)$")):
    run = RUNS.get(run_id) or load_b2_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    if format == "json":
        return Response(
            json.dumps(run, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="brandblaze-{run_id}.json"'},
        )
    output = io.StringIO()
    fields = [
        "run_id", "product_name", "variant_id", "market", "channel", "environment",
        "status", "score", "critical_drift", "qa_violations", "qa_notes",
        "qa_axes", "failed_constraint_ids", "approval_status", "approval_note",
        "sha256", "durable_url", "provider",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for variant in run.get("variants", []):
        writer.writerow({
            "run_id": run_id,
            "product_name": run.get("product_name"),
            "variant_id": variant.get("id"),
            "market": variant.get("market"),
            "channel": variant.get("channel"),
            "environment": variant.get("environment"),
            "status": variant.get("status"),
            "score": variant.get("score"),
            "critical_drift": variant.get("critical_drift", False),
            "qa_violations": " | ".join(variant.get("qa_violations") or []),
            "qa_notes": variant.get("qa_notes"),
            "qa_axes": json.dumps(variant.get("qa_axes") or {}),
            "failed_constraint_ids": " | ".join(variant.get("failed_constraint_ids") or []),
            "approval_status": variant.get("approval_status", "pending"),
            "approval_note": variant.get("approval_note"),
            "sha256": variant.get("sha256"),
            "durable_url": variant.get("durable_url"),
            "provider": variant.get("provider"),
        })
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="brandblaze-{run_id}.csv"'},
    )
