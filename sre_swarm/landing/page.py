"""
Marketing landing page for Autonomous SRE Orchestration Swarm (AegisSRE).
Served at GET / when run via the dashboard app.
"""

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<meta name="description" content="Agent-driven SRE that detects microservice failures, finds root cause, and orchestrates semantic rollbacks with Temporal and MCP."/>
<title>AegisSRE — Autonomous SRE Orchestration Swarm</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0a0c10;
    --surface: #12151c;
    --border: #1e293b;
    --text: #e2e8f0;
    --muted: #64748b;
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --green: #22c55e;
    --purple: #a5b4fc;
  }
  html { scroll-behavior: smooth; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    font-size: 16px;
    line-height: 1.6;
    background: var(--bg);
    color: var(--text);
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }

  .nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 24px; max-width: 1100px; margin: 0 auto;
    border-bottom: 1px solid var(--border);
  }
  .logo { font-weight: 700; font-size: 18px; color: #f8fafc; letter-spacing: -0.02em; }
  .logo span { color: var(--accent); }
  .nav-links { display: flex; gap: 24px; align-items: center; font-size: 14px; }
  .nav-links a { color: var(--muted); }
  .nav-links a:hover { color: var(--text); text-decoration: none; }

  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: var(--accent); color: #fff; border: none;
    padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 600;
    cursor: pointer; text-decoration: none;
  }
  .btn:hover { background: var(--accent-hover); text-decoration: none; color: #fff; }
  .btn-outline {
    background: transparent; border: 1px solid var(--border); color: var(--text);
  }
  .btn-outline:hover { background: var(--surface); border-color: #334155; color: #fff; }

  .hero {
    max-width: 1100px; margin: 0 auto; padding: 72px 24px 56px;
    text-align: center;
  }
  .badge-pill {
    display: inline-block; padding: 4px 12px; border-radius: 999px;
    font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
    background: #1e3a5f; color: #60a5fa; margin-bottom: 20px;
  }
  .hero h1 {
    font-size: clamp(2rem, 5vw, 3rem); font-weight: 700; line-height: 1.15;
    color: #f8fafc; letter-spacing: -0.03em; max-width: 720px; margin: 0 auto 16px;
  }
  .hero p.lead {
    font-size: 18px; color: var(--muted); max-width: 560px; margin: 0 auto 32px;
  }
  .hero-cta { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }

  section { padding: 56px 24px; max-width: 1100px; margin: 0 auto; }
  section h2 {
    font-size: 24px; font-weight: 600; color: #f8fafc; margin-bottom: 8px;
  }
  .section-sub { color: var(--muted); font-size: 15px; margin-bottom: 32px; }

  .pillars {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px;
  }
  .pillar {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px;
  }
  .pillar h3 { font-size: 15px; font-weight: 600; color: #f8fafc; margin-bottom: 4px; }
  .pillar .tech { font-size: 12px; color: var(--purple); font-weight: 600; margin-bottom: 8px; }
  .pillar p { font-size: 13px; color: var(--muted); line-height: 1.5; }

  .flow {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; font-family: "JetBrains Mono", "Fira Code", ui-monospace, monospace;
    font-size: 13px; line-height: 1.7; color: #94a3b8; overflow-x: auto;
  }
  .flow .highlight { color: #60a5fa; }
  .flow .arrow { color: var(--muted); }

  .steps { counter-reset: step; list-style: none; }
  .steps li {
    position: relative; padding-left: 36px; margin-bottom: 16px; font-size: 14px; color: #cbd5e1;
  }
  .steps li::before {
    counter-increment: step; content: counter(step);
    position: absolute; left: 0; top: 0;
    width: 24px; height: 24px; border-radius: 6px;
    background: #1e293b; color: var(--accent); font-size: 12px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
  }
  .steps code {
    font-family: ui-monospace, monospace; font-size: 12px;
    background: #1e293b; padding: 2px 6px; border-radius: 4px; color: #e2e8f0;
  }

  footer {
    border-top: 1px solid var(--border); padding: 32px 24px; text-align: center;
    font-size: 13px; color: var(--muted);
  }
  footer a { color: var(--muted); }
  footer a:hover { color: var(--text); }
</style>
</head>
<body>

<nav class="nav">
  <div class="logo">⚡ Aegis<span>SRE</span></div>
  <div class="nav-links">
    <a href="#pillars">Design</a>
    <a href="#architecture">Architecture</a>
    <a href="#start">Quick start</a>
    <a href="https://github.com/soggy8/AegisSRE" target="_blank" rel="noopener">GitHub</a>
    <a class="btn" href="/dashboard">Open dashboard</a>
  </div>
</nav>

<header class="hero">
  <div class="badge-pill">Proof of concept</div>
  <h1>Autonomous incident response for distributed microservices</h1>
  <p class="lead">
    Detect failures from real telemetry, analyze root cause with agents, and orchestrate
    semantic rollbacks—with human approval in the loop—using durable Temporal workflows and MCP tools.
  </p>
  <div class="hero-cta">
    <a class="btn" href="/dashboard">Launch dashboard</a>
    <a class="btn btn-outline" href="#start">Run locally</a>
  </div>
</header>

<section id="pillars">
  <h2>Built on five pillars</h2>
  <p class="section-sub">Each layer has a clear boundary: workflows stay deterministic; side effects live in activities.</p>
  <div class="pillars">
    <article class="pillar">
      <h3>Durable execution</h3>
      <div class="tech">Temporal.io</div>
      <p>Fault-oblivious orchestration loops survive process restarts and network blips.</p>
    </article>
    <article class="pillar">
      <h3>Tool calling</h3>
      <div class="tech">MCP (stateless)</div>
      <p>Agents reach infrastructure through a self-describing HTTP tool surface.</p>
    </article>
    <article class="pillar">
      <h3>Long-running actions</h3>
      <div class="tech">MCP Tasks</div>
      <p>Async remediation and human-in-the-loop approvals without blocking the workflow.</p>
    </article>
    <article class="pillar">
      <h3>Observability</h3>
      <div class="tech">eBPF mock + OpenTelemetry</div>
      <p>Ground-truth traces and fixtures drive realistic incident scenarios.</p>
    </article>
    <article class="pillar">
      <h3>Rollbacks</h3>
      <div class="tech">Saga pattern</div>
      <p>Compensating transactions—no two-phase commit required.</p>
    </article>
  </div>
</section>

<section id="architecture">
  <h2>Repository layout</h2>
  <p class="section-sub">Independent packages so teams can evolve workflows, activities, and telemetry in parallel.</p>
  <pre class="flow"><span class="highlight">sre_swarm/</span>
├── <span class="highlight">workflows/</span>       <span class="arrow"># Deterministic Temporal workflow definitions</span>
├── <span class="highlight">activities/</span>      <span class="arrow"># LLM calls, HTTP, mutations (all I/O here)</span>
├── <span class="highlight">mcp/</span>             <span class="arrow"># Stateless MCP server</span>
├── <span class="highlight">telemetry/</span>       <span class="arrow"># Mock eBPF + OTLP ingestion</span>
├── <span class="highlight">dashboard/</span>       <span class="arrow"># Live incidents + webhook intake</span>
└── worker.py        <span class="arrow"># Temporal worker entrypoint</span></pre>
</section>

<section id="start">
  <h2>Quick start</h2>
  <p class="section-sub">After dependencies and Temporal are running, open the dashboard to trigger a test incident.</p>
  <ol class="steps">
    <li><code>pip install -r requirements.txt</code> and copy <code>.env.example</code> to <code>.env</code></li>
    <li>Start Temporal (<code>docker run … temporalio/auto-setup</code>), mock services, MCP server, and worker</li>
    <li>Run <code>python -m sre_swarm.dashboard.app</code> and visit <a href="/dashboard">/dashboard</a></li>
    <li>Click <strong>Trigger incident</strong>, then approve or reject the proposed rollback</li>
  </ol>
  <p style="margin-top:24px;font-size:14px;color:var(--muted);">
    Full commands and port map are in the
    <a href="https://github.com/soggy8/AegisSRE#quick-start" target="_blank" rel="noopener">README on GitHub</a>.
  </p>
</section>

<footer>
  AegisSRE · Autonomous SRE Orchestration Swarm ·
  <a href="https://github.com/soggy8/AegisSRE" target="_blank" rel="noopener">Source on GitHub</a>
</footer>

</body>
</html>
"""
