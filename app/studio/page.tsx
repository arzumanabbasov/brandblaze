"use client";

import { ChangeEvent, useEffect, useMemo, useState } from "react";
import Link from "next/link";

type Variant = {
  id: string;
  label: string;
  market: string;
  channel: string;
  environment: string;
  objective?: string;
  treatment?: string;
  style?: string;
  status: "queued" | "generating" | "reviewing" | "ready" | "flagged" | "failed";
  url?: string;
  provider?: string;
  score?: number;
  qa_notes?: string;
  critical_drift?: boolean;
  qa_violations?: string[];
  qa_axes?: Record<string, number>;
  failed_constraint_ids?: string[];
  repair_plan?: { repair_instruction?: string; retry_recommended?: boolean };
  approval_status?: "pending" | "approved" | "rejected";
  approval_note?: string;
  sha256?: string;
  attempts?: Attempt[];
};

type Attempt = {
  attempt: number;
  url?: string;
  durable_url?: string;
  sha256?: string;
  manifest_url?: string;
  score?: number;
  qa_notes?: string;
  critical_drift?: boolean;
  qa_violations?: string[];
  qa_axes?: Record<string, number>;
  failed_constraint_ids?: string[];
  repair_plan?: { repair_instruction?: string; retry_recommended?: boolean };
  prompt?: string;
  outcome: "accepted" | "rejected";
};

type ReactorRun = {
  run_id: string;
  status: "queued" | "running" | "complete" | "failed";
  variants: Variant[];
  manifest_urls?: string[];
  identity_map?: string;
  identity_spec?: { version: number; constraints: Array<{ id: string; dimension: string; description: string; confidence: number; hard: boolean }> };
  source_url?: string;
  product_name?: string;
  created_at?: string;
  qa_threshold?: number;
  max_attempts?: number;
  error?: string;
};

type RunSummary = {
  run_id: string;
  product_name?: string;
  status: string;
  aesthetic?: string;
  created_at?: string;
  variant_count: number;
  ready_count: number;
  flagged_count: number;
};

type Health = {
  genblaze: boolean;
  backblaze_b2: boolean;
  creative_director: string | null;
  image_provider: string | null;
  qa_threshold: number;
  max_attempts: number;
};

const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ??
  (process.env.NODE_ENV === "production" ? "" : "http://localhost:8000");

const markets = ["United States", "Japan", "France", "Brazil", "United Kingdom", "South Korea", "UAE", "Italy", "Germany", "India"];
const channels = ["Instagram", "Amazon", "Billboard", "Editorial", "TikTok", "Pinterest", "E-commerce PDP", "Print campaign", "Retail display", "Email"];
const environments = ["Studio", "Tokyo night", "Alpine morning", "Coastal summer", "Museum plinth", "Desert dusk", "Rainy city", "Botanical glasshouse", "Luxury hotel", "Modern kitchen"];
const aesthetics = ["Auto-diversify", "Quiet luxury", "Editorial surrealism", "Kinetic color", "Future naturalism", "Neo-classical", "Cinematic noir", "Soft minimalism", "Maximalist pop"];

function toggle(list: string[], value: string, setter: (value: string[]) => void) {
  setter(list.includes(value) ? list.filter((item) => item !== value) : [...list, value]);
}

export default function StudioPage() {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [productName, setProductName] = useState("");
  const [brief, setBrief] = useState("");
  const [selectedMarkets, setSelectedMarkets] = useState(["United States", "Japan"]);
  const [selectedChannels, setSelectedChannels] = useState(["Instagram", "Amazon"]);
  const [selectedEnvironments, setSelectedEnvironments] = useState(["Studio", "Tokyo night"]);
  const [aesthetic, setAesthetic] = useState("Auto-diversify");
  const [run, setRun] = useState<ReactorRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveRuns, setArchiveRuns] = useState<RunSummary[]>([]);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [selectedVariant, setSelectedVariant] = useState<Variant | null>(null);
  const [setupStep, setSetupStep] = useState(1);

  const plannedCount = useMemo(
    () => Math.min(selectedMarkets.length * selectedChannels.length * selectedEnvironments.length, 12),
    [selectedMarkets, selectedChannels, selectedEnvironments],
  );
  const requestedCount = selectedMarkets.length * selectedChannels.length * selectedEnvironments.length;
  const baselineClaudeCalls = 1 + plannedCount * 2;
  const maximumClaudeCalls = 1 + plannedCount * 3;

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/health`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((value: Health) => setHealth(value))
      .catch(() => setHealth(null));
    return () => controller.abort();
  }, []);

  function onFile(event: ChangeEvent<HTMLInputElement>) {
    const next = event.target.files?.[0] ?? null;
    setFile(next);
    setPreview(next ? URL.createObjectURL(next) : null);
    setError("");
  }

  async function startReactor() {
    if (!file) {
      setError("Add one clear product photograph first.");
      return;
    }
    if (!productName.trim() || !brief.trim()) {
      setError("Add the product name and a concise identity lock.");
      return;
    }
    if (!plannedCount) {
      setError("Select at least one market, channel, and environment.");
      return;
    }

    setBusy(true);
    setError("");
    const body = new FormData();
    body.append("file", file);
    body.append("product_name", productName);
    body.append("brief", brief);
    body.append("markets", JSON.stringify(selectedMarkets));
    body.append("channels", JSON.stringify(selectedChannels));
    body.append("environments", JSON.stringify(selectedEnvironments));
    body.append("aesthetic", aesthetic);

    try {
      const response = await fetch(`${API_URL}/api/runs`, { method: "POST", body });
      if (!response.ok) throw new Error(await response.text());
      const created = (await response.json()) as ReactorRun;
      setRun(created);
      pollRun(created.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The reactor could not start.");
      setBusy(false);
    }
  }

  function continueSetup() {
    setError("");
    if (setupStep === 1) {
      if (!file) return setError("Select one clear product photograph to continue.");
      if (!productName.trim()) return setError("Enter the product name to continue.");
      if (!brief.trim()) return setError("Describe the product details that must never change.");
    }
    if (setupStep === 2 && !plannedCount) {
      return setError("Select at least one market, channel, and environment.");
    }
    setSetupStep((current) => Math.min(3, current + 1));
  }

  function pollRun(runId: string) {
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(`${API_URL}/api/runs/${runId}`);
        if (!response.ok) throw new Error("Run status unavailable");
        const next = (await response.json()) as ReactorRun;
        setRun(next);
        if (next.status === "complete" || next.status === "failed") {
          window.clearInterval(timer);
          setBusy(false);
          if (next.status === "failed") {
            setError(next.error || "Generation failed before producing an image.");
          } else if (next.error) {
            setError(`Some variants failed: ${next.error}`);
          }
        }
      } catch {
        window.clearInterval(timer);
        setBusy(false);
        setError("Lost contact with the generation service.");
      }
    }, 1800);
  }

  async function openArchive() {
    setArchiveOpen(true);
    setArchiveBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/runs?include_b2=true&limit=40`);
      if (!response.ok) throw new Error(await response.text());
      setArchiveRuns(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load the B2 archive.");
    } finally {
      setArchiveBusy(false);
    }
  }

  async function openArchivedRun(runId: string) {
    try {
      const response = await fetch(`${API_URL}/api/runs/${runId}`);
      if (!response.ok) throw new Error(await response.text());
      const archived = (await response.json()) as ReactorRun;
      setRun(archived);
      setProductName(archived.product_name ?? "Archived product");
      setPreview(archived.source_url ?? null);
      setArchiveOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not open the archived run.");
    }
  }

  async function decideVariant(variantId: string, action: "approve" | "reject") {
    if (!run) return;
    const note = action === "reject" ? window.prompt("Optional rejection note for the campaign record:", "") ?? "" : "";
    try {
      const response = await fetch(`${API_URL}/api/runs/${run.run_id}/variants/${variantId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, note }),
      });
      if (!response.ok) throw new Error(await response.text());
      const next = (await response.json()) as ReactorRun;
      setRun(next);
      setSelectedVariant(next.variants.find((item) => item.id === variantId) ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save the campaign decision.");
    }
  }

  async function regenerateVariant(variantId: string) {
    if (!run) return;
    try {
      const response = await fetch(`${API_URL}/api/runs/${run.run_id}/variants/${variantId}/regenerate`, { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      setRun(await response.json());
      setSelectedVariant(null);
      setBusy(true);
      pollRun(run.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not regenerate this asset.");
    }
  }

  const shownVariants = run?.variants ?? [];
  const readyCount = run?.variants.filter((item) => item.status === "ready").length ?? 0;
  const flaggedCount = run?.variants.filter((item) => item.status === "flagged").length ?? 0;
  const queuedCount = run?.variants.filter((item) => ["queued", "generating", "reviewing"].includes(item.status)).length ?? 0;
  const scoredVariants = shownVariants.filter((item) => item.score != null);
  const averageScore = scoredVariants.length
    ? Math.round(scoredVariants.reduce((sum, item) => sum + (item.score ?? 0), 0) / scoredVariants.length)
    : 0;
  const recoveredCount = shownVariants.filter((item) => item.status === "ready" && (item.attempts?.length ?? 0) > 1).length;
  const approvedCount = shownVariants.filter((item) => item.approval_status === "approved").length;
  const groupedVariants = Object.entries(
    shownVariants.reduce<Record<string, Variant[]>>((groups, variant) => {
      (groups[variant.market] ??= []).push(variant);
      return groups;
    }, {}),
  );
  const pipelineSteps = ["Ingest to B2", "Identity contract", "GMI generation", "Constraint QA", "Commit manifests"];
  const stageClass = (index: number) => {
    if (!run) return "";
    if (run.status === "complete") return "done";
    if (run.status === "failed") return index === 0 ? "done" : "failed";
    if (index === 0) return "done";
    if (index === 1) return run.identity_map ? "done" : "active";
    if (index === 2) return run.identity_map ? "active" : "";
    if (index === 3 && run.variants.some((item) => item.status === "ready")) return "active";
    return "";
  };

  return (
    <main>
      <nav className="nav">
        <Link className="brand" href="/"><span className="brandMark">BB</span><span>BRANDBLAZE</span></Link>
        <div className="navMeta">
          <span className={`statusDot ${health ? "" : "offline"}`} />
          {health?.genblaze && health.backblaze_b2 && health.creative_director && health.image_provider
            ? "Pipeline configured"
            : health ? "Setup incomplete" : "Backend offline"}
        </div>
        <button className="ghostButton" type="button" onClick={openArchive}>B2 run archive <span>↗</span></button>
      </nav>

      <section className="studioHeader">
        <div><span>PRODUCTION STUDIO</span><h1>Build a campaign</h1></div>
        <p>Configure one source, review the provider budget, then generate. Your setup remains editable until the run starts.</p>
      </section>

      <section className="workspace">
        <aside className="controlPanel">
          <div className="setupSteps" aria-label="Campaign setup progress">
            {["Product", "Campaign", "Review"].map((label, index) => (
              <button className={setupStep === index + 1 ? "active" : setupStep > index + 1 ? "complete" : ""} type="button" key={label} onClick={() => index + 1 < setupStep && setSetupStep(index + 1)}>
                <i>{setupStep > index + 1 ? "✓" : index + 1}</i><span>{label}</span>
              </button>
            ))}
          </div>

          {setupStep === 1 && (
            <div className="setupPanel">
              <div className="panelTitle"><span>01</span><h2>Canonical product</h2></div>
              <p className="stepIntro">Choose the reference that every campaign variant must preserve.</p>
              <label className={`dropzone ${preview ? "hasPreview" : ""}`}>
                <input type="file" accept="image/png,image/jpeg,image/webp" onChange={onFile} />
                {/* eslint-disable-next-line @next/next/no-img-element */}
                {preview ? <img src={preview} alt="Uploaded product preview" /> : (
                  <div><strong>Choose the product photograph</strong><span>PNG, JPG or WEBP · maximum 20 MB</span><b>＋ Select image</b></div>
                )}
              </label>
              <label className="field"><span>Product name</span><input placeholder="e.g. Aurora porcelain tea set" value={productName} onChange={(e) => setProductName(e.target.value)} /></label>
              <label className="field"><span>Identity lock</span><textarea rows={4} placeholder="Describe the exact shape, components, colors, patterns, materials, and markings that must remain unchanged." value={brief} onChange={(e) => setBrief(e.target.value)} /></label>
            </div>
          )}

          {setupStep === 2 && (
            <div className="setupPanel">
              <div className="panelTitle"><span>02</span><h2>Campaign matrix</h2></div>
              <p className="stepIntro">Select the dimensions you need. BrandBlaze balances coverage across markets, channels, and scenes.</p>
              <ChoiceGroup number="A" title="Markets" options={markets} selected={selectedMarkets} onToggle={(value) => toggle(selectedMarkets, value, setSelectedMarkets)} />
              <ChoiceGroup number="B" title="Channels" options={channels} selected={selectedChannels} onToggle={(value) => toggle(selectedChannels, value, setSelectedChannels)} />
              <ChoiceGroup number="C" title="Environments" options={environments} selected={selectedEnvironments} onToggle={(value) => toggle(selectedEnvironments, value, setSelectedEnvironments)} />
              <div className="choiceGroup">
                <div className="panelTitle small"><span>D</span><h2>Creative strategy</h2></div>
                <p className="stepIntro">Auto-diversify gives every campaign slot a distinct visual style. Choose one style only when you intentionally want a unified art direction.</p>
                <div className="aestheticGrid">
                  {aesthetics.map((item, index) => (
                    <button type="button" className={aesthetic === item ? "selected" : ""} onClick={() => setAesthetic(item)} key={item}>
                      <i className={`swatch swatch${(index % 4) + 1}`} /><span>{item}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {setupStep === 3 && (
            <div className="setupPanel reviewPanel">
              <div className="panelTitle"><span>03</span><h2>Review and generate</h2></div>
              <p className="stepIntro">Confirm the scope and maximum provider usage before starting paid work.</p>
              <dl className="reviewList">
                <div><dt>Product</dt><dd>{productName}</dd></div>
                <div><dt>Markets</dt><dd>{selectedMarkets.join(", ")}</dd></div>
                <div><dt>Channels</dt><dd>{selectedChannels.join(", ")}</dd></div>
                <div><dt>Scenes</dt><dd>{selectedEnvironments.join(", ")}</dd></div>
                <div><dt>Strategy</dt><dd>{aesthetic}</dd></div>
              </dl>
              <div className="costPreview">
                <b>Maximum run budget</b>
                <span>{plannedCount}–{plannedCount * 2} GMI images</span>
                <span>{baselineClaudeCalls}–{maximumClaudeCalls} Claude calls</span>
                <small>Up to {(health?.max_attempts ?? 2) - 1} corrective retry is allowed when identity QA falls below {health?.qa_threshold ?? 85}%.</small>
              </div>
              <button className="reactButton" type="button" onClick={startReactor} disabled={busy}>
                <span>{busy ? "Reactor running" : "Generate campaign"}</span>
                <b>{plannedCount} variants{requestedCount > 12 ? " · balanced plan" : ""}</b>
              </button>
              <p className="pipelineNote">Your source is ingested to B2 only after you start the run.</p>
            </div>
          )}

          {error && <p className="error">{error}</p>}
          <div className="setupActions">
            {setupStep > 1 && !busy && <button type="button" onClick={() => { setError(""); setSetupStep((step) => step - 1); }}>Back</button>}
            {setupStep < 3 && <button className="nextButton" type="button" onClick={continueSetup}>Continue <span>→</span></button>}
          </div>
        </aside>

        <section className="reactor">
          <div className="reactorHeader">
            <div>
              <span className="sectionKicker">LIVE ASSET MATRIX</span>
              <h2>
                {run ? productName : "Untitled product"} / {selectedMarkets.length === 1
                  ? selectedMarkets[0]
                  : `${selectedMarkets.length} market campaign`}
              </h2>
            </div>
            <div className="runStats">
              <span><b>{queuedCount}</b> in progress</span>
              <span><b>{readyCount}</b> verified</span>
              <span><b>{flaggedCount}</b> flagged</span>
              <span><b>{run?.manifest_urls?.length ?? 0}</b> manifests</span>
            </div>
          </div>

          <div className="pipeline">
            {pipelineSteps.map((step, index) => (
              <div className={stageClass(index)} key={step}><i>{stageClass(index) === "done" ? "✓" : index + 1}</i><span>{step}</span></div>
            ))}
          </div>
          {run?.error && <p className="runError"><b>Run notice:</b> {run.error}</p>}
          {run?.identity_map && (
            <details className="identityMap">
              <summary>Machine-readable identity contract <span>{run.identity_spec?.constraints.length ?? 0} traceable constraints</span></summary>
              <pre>{run.identity_map}</pre>
            </details>
          )}
          {run && shownVariants.length > 0 && (
            <div className="qualitySummary">
              <span><b>{averageScore || "—"}%</b> average identity</span>
              <span><b>{readyCount}/{shownVariants.length}</b> machine verified</span>
              <span><b>{recoveredCount}</b> recovered by repair</span>
              <span><b>{approvedCount}</b> client approved</span>
            </div>
          )}
          {run && (
            <div className="exportBar">
              <span>Campaign handoff</span>
              <a href={`${API_URL}/api/runs/${run.run_id}/export?format=json`}>Export JSON</a>
              <a href={`${API_URL}/api/runs/${run.run_id}/export?format=csv`}>Export CSV</a>
            </div>
          )}

          <div className="campaignBoard">
            {!shownVariants.length && (
              <div className="emptyMatrix">
                <span>BB / 00</span>
                <h3>Your generated asset matrix will appear here.</h3>
                <p>Upload a real product image, choose a small first batch, and start the reactor.</p>
              </div>
            )}
            {groupedVariants.map(([market, variants]) => (
              <section className="marketGroup" key={market}>
                <div className="marketHeading"><h3>{market}</h3><span>{variants.length} campaign assets</span></div>
                <div className="assetGrid">
                  {variants.map((variant, index) => (
                    <article className={`assetCard ${variant.status}`} key={variant.id}>
                      <button className={`art art${(index % 8) + 1}`} type="button" onClick={() => variant.url && setSelectedVariant(variant)}>
                        {variant.url ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={variant.url} alt={`${productName}: ${variant.label}`} />
                        ) : run && preview ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img className="pendingReference" src={preview} alt="Source image awaiting generation" />
                        ) : <span className="awaitingAsset">Awaiting<br />generation</span>}
                        <span className="score">
                          {variant.critical_drift
                            ? "critical drift"
                            : variant.score != null
                              ? `${variant.score}% identity`
                              : variant.status === "failed" ? "generation failed" : variant.status}
                        </span>
                        {(variant.attempts?.length ?? 0) > 1 && <span className="retryBadge">{variant.attempts?.length} attempts</span>}
                      </button>
                      <div className="assetInfo">
                        <div>
                          <h3>{variant.label}</h3>
                          <p>{variant.market} · {variant.channel}</p>
                          {variant.objective && <p className="objective">{variant.objective}</p>}
                          {variant.treatment && <p className="treatment">{variant.treatment.split(" — ")[0]}</p>}
                          {variant.style && <p className="variantStyle">{variant.style}</p>}
                          {variant.qa_notes && <p className="qaNote">{variant.qa_notes}</p>}
                          {!!variant.qa_violations?.length && (
                            <ul className="violationList">
                              {variant.qa_violations.map((violation) => <li key={violation}>{violation}</li>)}
                            </ul>
                          )}
                        </div>
                        <div className="assetActions">
                          <span>{variant.approval_status === "approved" ? "client approved" : variant.approval_status === "rejected" ? "client rejected" : variant.status === "ready" ? "machine verified" : variant.status}</span>
                          {variant.url && <button type="button" onClick={() => setSelectedVariant(variant)}>Compare</button>}
                          {["ready", "flagged"].includes(variant.status) && variant.approval_status !== "approved" && <button type="button" onClick={() => decideVariant(variant.id, "approve")}>Approve</button>}
                          {["ready", "flagged"].includes(variant.status) && variant.approval_status !== "rejected" && <button type="button" onClick={() => decideVariant(variant.id, "reject")}>Reject</button>}
                          {["ready", "flagged", "failed"].includes(variant.status) && <button type="button" onClick={() => regenerateVariant(variant.id)}>Regenerate one</button>}
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              </section>
            ))}
          </div>

          {selectedVariant && (
            <div className="modalBackdrop" role="presentation" onClick={() => setSelectedVariant(null)}>
              <section className="compareModal" role="dialog" aria-modal="true" aria-label="Identity comparison" onClick={(event) => event.stopPropagation()}>
                <header><div><span>IDENTITY INSPECTOR</span><h2>{selectedVariant.label}</h2></div><button type="button" onClick={() => setSelectedVariant(null)}>×</button></header>
                <div className="compareGrid">
                  <figure>
                    {run?.source_url || preview ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={run?.source_url || preview || ""} alt="Canonical source product" />
                    ) : null}
                    <figcaption>Canonical source</figcaption>
                  </figure>
                  <figure>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={selectedVariant.url} alt="Generated campaign variant" />
                    <figcaption>Generated output</figcaption>
                  </figure>
                </div>
                <div className={`qaPanel ${selectedVariant.critical_drift ? "critical" : ""}`}>
                  <b>{selectedVariant.score ?? "—"}% identity</b>
                  <div>
                    <p>{selectedVariant.qa_notes}</p>
                    {!!selectedVariant.qa_violations?.length && (
                      <ul>{selectedVariant.qa_violations.map((violation) => <li key={violation}>{violation}</li>)}</ul>
                    )}
                  </div>
                  <span>{selectedVariant.attempts?.length ?? 1} attempt(s) · {selectedVariant.status === "ready" ? "verified" : selectedVariant.status}</span>
                </div>
                {!!selectedVariant.qa_axes && (
                  <div className="axisGrid">
                    {Object.entries(selectedVariant.qa_axes).map(([axis, score]) => (
                      <span className={score < (run?.qa_threshold ?? 85) ? "axisFail" : ""} key={axis}>
                        <b>{score}</b>{axis.replaceAll("_", " ")}
                      </span>
                    ))}
                  </div>
                )}
                <details className="lineageInspector">
                  <summary>Prompt and lineage inspector</summary>
                  <dl>
                    <div><dt>Output SHA-256</dt><dd>{selectedVariant.sha256 || "Unavailable"}</dd></div>
                    <div><dt>Failed constraints</dt><dd>{selectedVariant.failed_constraint_ids?.join(", ") || "None"}</dd></div>
                    <div><dt>Repair directive</dt><dd>{selectedVariant.repair_plan?.repair_instruction || "No repair required"}</dd></div>
                  </dl>
                  {selectedVariant.attempts?.map((attempt) => (
                    <details key={attempt.attempt}>
                      <summary>Attempt {attempt.attempt} · {attempt.outcome} · {attempt.score ?? "QA unavailable"}</summary>
                      <pre>{attempt.prompt}</pre>
                      {attempt.manifest_url && <p>Manifest: {attempt.manifest_url}</p>}
                    </details>
                  ))}
                </details>
                <div className="modalActions">
                  <button type="button" onClick={() => decideVariant(selectedVariant.id, "approve")}>Approve asset</button>
                  <button type="button" onClick={() => decideVariant(selectedVariant.id, "reject")}>Reject asset</button>
                  <button type="button" onClick={() => regenerateVariant(selectedVariant.id)}>Regenerate only this asset</button>
                </div>
                {selectedVariant.attempts && selectedVariant.attempts.length > 1 && (
                  <div className="attemptTimeline">
                    {selectedVariant.attempts.map((attempt) => <span className={attempt.outcome} key={attempt.attempt}>Attempt {attempt.attempt}: {attempt.score ?? "QA unavailable"} · {attempt.outcome}</span>)}
                  </div>
                )}
              </section>
            </div>
          )}

          {archiveOpen && (
            <div className="archiveDrawer">
              <header><div><span>BACKBLAZE B2</span><h2>Campaign archive</h2></div><button type="button" onClick={() => setArchiveOpen(false)}>×</button></header>
              {archiveBusy ? <p>Reading B2 indexes…</p> : archiveRuns.map((item) => (
                <button className="archiveRun" type="button" key={item.run_id} onClick={() => openArchivedRun(item.run_id)}>
                  <span><b>{item.product_name || "Untitled product"}</b><small>{item.run_id} · {item.created_at ? new Date(item.created_at).toLocaleString() : "date unavailable"}</small></span>
                  <span>{item.ready_count} verified · {item.flagged_count} flagged</span>
                </button>
              ))}
              {!archiveBusy && !archiveRuns.length && <p>No persisted campaigns found.</p>}
            </div>
          )}

          <footer className="vaultBar">
            <div><span className="vaultIcon">B2</span><p><b>Backblaze asset vault</b><small>Content-addressed originals, variants, and SHA-256 manifests</small></p></div>
            <p className="hash">{run?.run_id ? `run/${run.run_id}` : "Waiting for a verified manifest"}</p>
          </footer>
        </section>
      </section>
    </main>
  );
}

function ChoiceGroup({ number, title, options, selected, onToggle }: { number: string; title: string; options: string[]; selected: string[]; onToggle: (value: string) => void }) {
  const [custom, setCustom] = useState("");
  const visibleOptions = [...new Set([...options, ...selected])];
  function addCustom() {
    const value = custom.trim();
    if (value && !selected.includes(value)) onToggle(value);
    setCustom("");
  }
  return (
    <div className="choiceGroup">
      <div className="panelTitle small"><span>{number}</span><h2>{title}</h2><b className="choiceCount">{selected.length} selected</b></div>
      <div className="chips">{visibleOptions.map((option) => <button type="button" className={selected.includes(option) ? "selected" : ""} onClick={() => onToggle(option)} key={option}>{option}<i>{selected.includes(option) ? "×" : "+"}</i></button>)}</div>
      <div className="customChoice">
        <input value={custom} onChange={(event) => setCustom(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addCustom(); } }} placeholder={`Custom ${title.toLowerCase()}`} />
        <button type="button" onClick={addCustom} disabled={!custom.trim()}>Add</button>
      </div>
    </div>
  );
}
