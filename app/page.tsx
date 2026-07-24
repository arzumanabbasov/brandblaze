import Link from "next/link";

export default function LandingPage() {
  return (
    <main className="landing">
      <nav className="landingNav">
        <Link className="brand" href="/"><span className="brandMark">BB</span><span>BRANDBLAZE</span></Link>
        <div className="landingLinks">
          <a href="#workflow">How it works</a>
          <a href="#proof">Why B2</a>
          <Link className="landingNavCta" href="/studio">Open studio</Link>
        </div>
      </nav>

      <section className="landingHero">
        <p className="eyebrow">SELF-CORRECTING GENERATIVE CAMPAIGNS</p>
        <h1>One product.<br />Every market.<br /><em>Identity intact.</em></h1>
        <p>BrandBlaze generates market-ready product imagery, rejects identity drift, retries failed variants, and preserves every source, attempt, hash, manifest, and decision in Backblaze B2.</p>
        <div className="landingActions">
          <Link className="primaryCta" href="/studio">Build a campaign <span>→</span></Link>
          <a className="secondaryCta" href="#workflow">See the pipeline</a>
        </div>
        <div className="landingSignal">
          <span>CLAUDE VISION</span><i>→</i><span>GENBLAZE</span><i>→</i><span>GMI CLOUD</span><i>→</i><span>BACKBLAZE B2</span>
        </div>
      </section>

      <section className="landingProblem">
        <span className="landingIndex">01 / THE PROBLEM</span>
        <div>
          <h2>AI can make a beautiful ad while quietly destroying the product.</h2>
          <p>Geometry shifts. Patterns mutate. Components disappear. A campaign needs variation in the world around the product—not variation in the product itself.</p>
        </div>
      </section>

      <section className="landingWorkflow" id="workflow">
        <header><span className="landingIndex">02 / THE PIPELINE</span><h2>Generate. Inspect. Reject. Correct.</h2></header>
        <div className="workflowCards">
          <article><b>01</b><h3>Lock identity</h3><p>Claude turns the source photograph into canonical constraints for geometry, materials, colors, patterns, markings, and component count.</p></article>
          <article><b>02</b><h3>Direct variants</h3><p>Markets, channels, environments, and aesthetics become authored commercial prompts while the product remains invariant.</p></article>
          <article><b>03</b><h3>Generate through Genblaze</h3><p>GMI Cloud edits the B2-backed source while Genblaze handles orchestration, polling, manifests, and storage.</p></article>
          <article><b>04</b><h3>Reject identity drift</h3><p>Claude compares source and output. Low scores trigger one corrective retry; unresolved drift is flagged, never verified.</p></article>
        </div>
      </section>

      <section className="landingProof" id="proof">
        <div>
          <span className="landingIndex">03 / DURABLE BY DESIGN</span>
          <h2>Not a folder of disposable AI images.</h2>
          <p>Every branch becomes an auditable media record: source specimen, prompts, rejected attempts, accepted outputs, SHA-256 hashes, QA decisions, and Genblaze manifests.</p>
          <Link className="textLink" href="/studio">Enter the production studio →</Link>
        </div>
        <div className="vaultDiagram">
          <span className="vaultRoot">B2 / CANONICAL SOURCE</span>
          <div><span>ATTEMPT 01</span><b>82 · REJECTED</b></div>
          <div><span>CORRECTIVE RETRY</span><b>93 · VERIFIED</b></div>
          <div><span>MANIFEST + SHA-256</span><b>COMMITTED</b></div>
        </div>
      </section>

      <section className="landingFinal">
        <p className="eyebrow">FROM ONE SPECIMEN TO A GOVERNED CAMPAIGN TREE</p>
        <h2>Build the world.<br /><em>Keep the product.</em></h2>
        <Link className="primaryCta" href="/studio">Launch BrandBlaze <span>→</span></Link>
      </section>

      <footer className="landingFooter"><span>BRANDBLAZE / 2026</span><span>Powered by Genblaze + Backblaze B2</span></footer>
    </main>
  );
}
