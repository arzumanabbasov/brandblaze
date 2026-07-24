from __future__ import annotations

import json
import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from genblaze_core import Asset, KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

app = FastAPI(title="BrandBlaze Genblaze API", version="0.1.0")
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
    status: str = "queued"
    url: str | None = None
    durable_url: str | None = None
    provider: str | None = None
    score: int | None = None
    qa_notes: str | None = None
    sha256: str | None = None


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


def provider_for():
    from genblaze_gmicloud import GMICloudImageProvider

    required("GMI_API_KEY")
    return GMICloudImageProvider(), os.getenv("GMI_IMAGE_MODEL", "seedream-5.0-lite"), "GMI Cloud / Seedream 5"


def claude_text(message) -> str:
    return "\n".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()


def analyze_product_with_claude(source_url: str, product_name: str, user_brief: str) -> str:
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=900,
        temperature=0.2,
        system=(
            "You are the visual continuity director for a world-class product photography studio. "
            "Inspect the reference with forensic care. Build a compact identity lock another image "
            "model can follow. Separate immutable product geometry from mutable art direction. Never "
            "invent features you cannot see. Return plain text under exactly these headings: "
            "IMMUTABLE IDENTITY, MATERIAL & SURFACE, LOGO/TYPOGRAPHY, CAMERA EVIDENCE, NEVER CHANGE."
        ),
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
    identity = claude_text(response)
    if not identity:
        raise RuntimeError("Claude returned an empty identity map.")
    return identity


def direct_variant_with_claude(
    identity_map: str,
    product_name: str,
    market: str,
    channel: str,
    environment: str,
    aesthetic: str,
) -> str:
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=1100,
        temperature=0.65,
        system=(
            "You are an elite commercial art director writing executable prompts for a high-end "
            "image-editing model. Create one decisive photographic concept, not options. The reference "
            "product must remain geometrically and materially faithful. Translate market context through "
            "light, space, color, props, and composition—never stereotypes, flags, tourist symbols, or "
            "written text. Specify lens, camera height, framing, lighting, palette, surface, depth, "
            "atmosphere, and product placement. Make it authored, tactile, expensive, and editorial—not "
            "generic AI art. Output only the final 180–260 word image prompt. Put identity constraints "
            "first and negative constraints last."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"PRODUCT: {product_name}\nMARKET: {market}\nCHANNEL: {channel}\n"
                f"ENVIRONMENT: {environment}\nAESTHETIC: {aesthetic}\n\n"
                f"CANONICAL IDENTITY LOCK:\n{identity_map}"
            ),
        }],
    )
    prompt = claude_text(response)
    if not prompt:
        raise RuntimeError("Claude returned an empty art-direction prompt.")
    return prompt


def evaluate_variant_with_claude(source_url: str, output_url: str) -> tuple[int, str]:
    response = Anthropic(api_key=required("ANTHROPIC_API_KEY")).messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=300,
        temperature=0,
        system=(
            "You are a strict product-continuity inspector. Compare the source product in image one "
            "with the generated product in image two. Ignore background, lighting, camera angle, and "
            "props. Score only product geometry, colors, materials, patterns, logos, and component count. "
            "Return valid JSON only: {\"score\": integer 0-100, \"notes\": \"one concise sentence\"}."
        ),
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "url", "url": source_url}},
                {"type": "image", "source": {"type": "url", "url": output_url}},
                {"type": "text", "text": "Evaluate product identity preservation."},
            ],
        }],
    )
    raw = claude_text(response).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    score = max(0, min(100, int(result["score"])))
    return score, str(result["notes"])[:500]


def update_variant(run_id: str, variant_id: str, **changes: Any) -> None:
    with RUN_LOCK:
        for variant in RUNS[run_id]["variants"]:
            if variant["id"] == variant_id:
                variant.update(changes)
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
):
    update_variant(run_id, variant.id, status="generating")
    provider, model, provider_label = provider_for()
    prompt = direct_variant_with_claude(
        identity_map,
        product_name,
        variant.market,
        variant.channel,
        variant.environment,
        aesthetic,
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
        Pipeline("brandblaze-variant", tenant_id=run_id)
        .step(provider, **step_params)
        .run(sink=storage_sink(), timeout=int(os.getenv("GENBLAZE_TIMEOUT", "300")))
    )

    if not result.run.steps or not result.run.steps[-1].assets:
        raise RuntimeError("GMI returned no image asset.")
    asset = result.run.steps[-1].assets[0]
    if not asset.url or not asset.sha256:
        raise RuntimeError("Generated asset is missing its B2 URL or SHA-256 digest.")
    verified = result.manifest.verify()
    if not verified:
        raise RuntimeError("Genblaze manifest verification failed.")
    display_url = presign_output_url(asset.url)
    score, qa_notes = evaluate_variant_with_claude(source_url, display_url)
    update_variant(
        run_id,
        variant.id,
        status="ready",
        url=display_url,
        durable_url=asset.url,
        provider=provider_label,
        score=score,
        qa_notes=qa_notes,
        sha256=asset.sha256,
    )
    return str(result.manifest.manifest_uri or "")


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
        identity_map = analyze_product_with_claude(source_url, product_name, brief)
        with RUN_LOCK:
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
                manifest_url = future.result()
                if manifest_url:
                    manifests.append(manifest_url)
            except Exception as exc:
                failures.append(f"{variant.label}: {exc}")
                update_variant(run_id, variant.id, status="failed")

    with RUN_LOCK:
        RUNS[run_id]["manifest_urls"] = manifests
        RUNS[run_id]["status"] = "complete" if manifests else "failed"
        if failures:
            RUNS[run_id]["error"] = " | ".join(failures)
        persist_run(run_id)

    # Persist the application-level index beside Genblaze's per-run manifests.
    key = f"brandblaze/indexes/{run_id}.json"
    try:
        index = json.loads(json.dumps(RUNS[run_id]))
        index["source_url"] = f"b2://{required('B2_BUCKET')}/{index['source_key']}"
        for variant in index["variants"]:
            variant["url"] = variant.get("durable_url")
        b2_client().put_object(
            Bucket=required("B2_BUCKET"),
            Key=key,
            Body=json.dumps(index, indent=2).encode(),
            ContentType="application/json",
        )
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
    }


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

    variants = [
        Variant(
            id=uuid.uuid4().hex[:10],
            label=f"{environment} / {channel}",
            market=market,
            channel=channel,
            environment=environment,
        )
        for market in parsed_markets
        for channel in parsed_channels
        for environment in parsed_environments
    ][: int(os.getenv("MAX_VARIANTS_PER_RUN", "12"))]

    RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "source_url": source_url,
        "source_key": source_key,
        "product_name": product_name,
        "aesthetic": aesthetic,
        "source_sha256": source_sha256,
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
        raise HTTPException(404, "Run not found.")
    return RUNS[run_id]
