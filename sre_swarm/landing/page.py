"""
Landing page for AegisSRE, served at GET / by the dashboard app.
"""

LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<meta name="description" content="AegisSRE pulls traces for a failing service, finds the root cause, and drafts the saga compensations to undo the damage. A human approves; Temporal makes sure it finishes."/>
<title>AegisSRE</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --paper: #f3f1ec;
    --paper-2: #e9e6df;
    --ink: #16150f;
    --ink-2: #4a473f;
    --ink-3: #8a867b;
    --rule: #d4d0c6;
    --signal: #e8491d;
    --ok: #2f7d4f;
    --term: #16150f;
    --term-text: #d9d5ca;
    --term-dim: #7d796e;
    --sans: "IBM Plex Sans", -apple-system, "Segoe UI", system-ui, sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: var(--sans);
    font-size: 16px;
    line-height: 1.55;
    background: var(--paper);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  a { color: inherit; }
  code, pre, .mono { font-family: var(--mono); }
  ::selection { background: var(--signal); color: var(--paper); }

  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 32px; }

  /* ---- top bar ---- */
  .bar {
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 22px 0; border-bottom: 1px solid var(--ink);
  }
  .mark { font-family: var(--mono); font-weight: 600; font-size: 15px; letter-spacing: -0.01em; text-decoration: none; }
  .mark i { font-style: normal; color: var(--signal); }
  .bar nav { display: flex; gap: 28px; font-size: 14px; }
  .bar nav a { text-decoration: none; color: var(--ink-2); }
  .bar nav a:hover { color: var(--ink); }
  .bar nav a.go { color: var(--ink); font-weight: 500; }

  /* ---- hero ---- */
  .hero {
    display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
    gap: 56px; padding: 72px 0 88px; align-items: start;
  }
  .eyebrow {
    font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    text-transform: uppercase; letter-spacing: .08em; margin-bottom: 20px;
  }
  .hero h1 {
    font-size: clamp(34px, 4.4vw, 52px); line-height: 1.04; font-weight: 600;
    letter-spacing: -0.035em; margin-bottom: 24px;
  }
  .hero h1 em { font-style: normal; color: var(--signal); }
  .hero p { color: var(--ink-2); font-size: 17px; max-width: 30em; margin-bottom: 32px; }
  .links { display: flex; gap: 24px; align-items: center; flex-wrap: wrap; font-size: 15px; }
  .btn {
    display: inline-block; background: var(--ink); color: var(--paper);
    padding: 11px 18px; text-decoration: none; font-weight: 500; border-radius: 2px;
  }
  .btn:hover { background: var(--signal); }
  .under { text-decoration: underline; text-underline-offset: 4px; text-decoration-color: var(--rule); }
  .under:hover { text-decoration-color: var(--ink); }

  /* ---- transcript ---- */
  .term {
    background: var(--term); color: var(--term-text); border-radius: 4px;
    font-family: var(--mono); font-size: 12px; line-height: 1.7;
    box-shadow: 0 1px 0 var(--ink), 12px 12px 0 var(--paper-2);
  }
  .term-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; border-bottom: 1px solid #2c2a23; color: var(--term-dim); font-size: 11.5px;
  }
  .term-head .state { color: var(--term-text); }
  .term-head .state b { font-weight: 500; }
  .term-head button {
    background: none; border: 0; color: var(--term-dim); font: inherit; cursor: pointer; padding: 0;
  }
  .term-head button:hover { color: var(--term-text); }
  .term-body { padding: 14px 18px 18px; overflow-x: auto; }
  .ln { display: grid; grid-template-columns: 100px 104px 1fr; white-space: pre; opacity: 0; transform: translateY(2px); transition: opacity .25s, transform .25s; }
  .ln.on { opacity: 1; transform: none; }
  .ln .t { color: var(--term-dim); }
  .ln .k { color: var(--term-dim); }
  .ln .k.hot { color: #ff7a4d; }
  .ln .k.good { color: #6fcf97; }
  .ln .err { color: #ff7a4d; }
  .ln .dim { color: var(--term-dim); }
  .ln .wait { color: #f2c94c; }
  .ln.gap { height: 8px; }

  /* ---- sections ---- */
  section { border-top: 1px solid var(--ink); padding: 64px 0 80px; }
  .sec-head { display: grid; grid-template-columns: minmax(0, 4fr) minmax(0, 8fr); gap: 56px; margin-bottom: 48px; }
  .sec-head .num { font-family: var(--mono); font-size: 12px; color: var(--ink-3); letter-spacing: .06em; }
  .sec-head h2 { font-size: 30px; line-height: 1.12; font-weight: 600; letter-spacing: -0.025em; margin-top: 8px; }
  .sec-head p { color: var(--ink-2); font-size: 17px; max-width: 36em; align-self: end; }

  /* lifecycle */
  .states { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-top: 1px solid var(--rule); }
  .state-col { padding: 20px 20px 0 0; position: relative; }
  .state-col + .state-col { padding-left: 20px; border-left: 1px solid var(--rule); }
  .state-col::before {
    content: ""; position: absolute; top: -4px; left: 0; width: 7px; height: 7px;
    background: var(--ink); border-radius: 50%;
  }
  .state-col + .state-col::before { left: -4px; }
  .state-col.hold::before { background: var(--signal); }
  .state-col.done::before { background: var(--ok); }
  .state-col h3 { font-family: var(--mono); font-size: 13px; font-weight: 600; margin-bottom: 10px; }
  .state-col.hold h3 { color: var(--signal); }
  .state-col p { font-size: 14px; color: var(--ink-2); margin-bottom: 12px; }
  .state-col .fact { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); }

  /* decisions */
  .decisions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 56px; }
  .decision { padding: 24px 0 28px; border-top: 1px solid var(--rule); }
  .decision h3 { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; margin-bottom: 8px; }
  .decision p { color: var(--ink-2); font-size: 15px; margin-bottom: 12px; }
  .decision .src { font-family: var(--mono); font-size: 12px; color: var(--ink-3); }

  /* intake + run */
  .split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 56px; }
  .split h3 { font-size: 18px; font-weight: 600; margin-bottom: 8px; letter-spacing: -0.01em; }
  .split > div > p { color: var(--ink-2); font-size: 15px; margin-bottom: 20px; }
  pre.code {
    background: var(--paper-2); border-left: 2px solid var(--ink);
    padding: 16px 18px; font-size: 12.5px; line-height: 1.7; overflow-x: auto; color: var(--ink);
  }
  pre.code .c { color: var(--ink-3); }
  pre.code .s { color: #9a3412; }
  ol.run { list-style: none; counter-reset: r; }
  ol.run li {
    counter-increment: r; display: grid; grid-template-columns: 28px 1fr; gap: 8px;
    padding: 10px 0; border-top: 1px solid var(--rule); font-size: 14px;
  }
  ol.run li::before { content: counter(r, decimal-leading-zero); font-family: var(--mono); font-size: 12px; color: var(--ink-3); padding-top: 2px; }
  ol.run code { font-size: 12.5px; }
  ol.run .note { display: block; color: var(--ink-3); font-size: 13px; }

  footer {
    border-top: 1px solid var(--ink); padding: 24px 0 40px;
    display: flex; justify-content: space-between; gap: 24px; flex-wrap: wrap;
    font-size: 13px; color: var(--ink-3);
  }
  footer a { color: var(--ink-2); }

  @media (max-width: 900px) {
    .hero, .sec-head, .split, .decisions { grid-template-columns: minmax(0, 1fr); gap: 32px; }
    .hero { padding: 48px 0 64px; }
    .states { grid-template-columns: 1fr; border-top: 0; }
    .state-col, .state-col + .state-col { padding: 0 0 24px 24px; border-left: 1px solid var(--rule); }
    .state-col::before, .state-col + .state-col::before { left: -4px; top: 6px; }
    .bar nav a:not(.go) { display: none; }
  }
  @media (max-width: 560px) {
    .wrap { padding: 0 20px; }
    .term { font-size: 11.5px; }
    .ln { grid-template-columns: 0 92px minmax(0, 1fr); }
    .ln .t { visibility: hidden; }
    .ln > span:last-child { white-space: pre-wrap; overflow-wrap: anywhere; }
  }
  @media (prefers-reduced-motion: reduce) {
    .ln { transition: none; }
  }
</style>
</head>
<body>
<div class="wrap">

<header class="bar">
  <a class="mark" href="/">aegis<i>/</i>sre</a>
  <nav>
    <a href="#lifecycle">How it works</a>
    <a href="#decisions">Design</a>
    <a href="#run">Run it</a>
    <a href="https://github.com/soggy8/AegisSRE" target="_blank" rel="noopener">Source</a>
    <a class="go" href="/dashboard">Dashboard &rarr;</a>
  </nav>
</header>

<div class="hero">
  <div>
    <div class="eyebrow">Incident response for microservices</div>
    <h1>The page fires. The rollback is <em>already drafted.</em></h1>
    <p>
      AegisSRE pulls the traces for a failing service, works out what broke, and writes the
      compensating calls that undo the damage. You approve it. Temporal makes sure it finishes,
      even if the worker dies halfway through.
    </p>
    <div class="links">
      <a class="btn" href="#run">Run it locally</a>
      <a class="under" href="https://github.com/soggy8/AegisSRE/blob/main/sre_swarm/workflows/incident_response.py" target="_blank" rel="noopener">Read the workflow</a>
    </div>
  </div>

  <div class="term" aria-label="Example incident transcript">
    <div class="term-head">
      <span>incident-INC-7F3A21C0</span>
      <span class="state">status: <b id="term-state">detecting</b></span>
      <button id="replay" type="button">replay</button>
    </div>
    <div class="term-body" id="term">
      <div class="ln"><span class="t">12:05:00.412</span><span class="k hot">alert</span><span>api-gateway 504, p99 4.8s <span class="dim">via alertmanager</span></span></div>
      <div class="ln" data-state="detecting"><span class="t">12:05:00.431</span><span class="k">detecting</span><span>get_telemetry_context <span class="dim">2 traces</span></span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span><span class="dim">api-gateway      </span> <span class="err">504</span>  4823ms</span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span><span class="dim">order-service    </span> 200    93ms</span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span><span class="dim">payment-service  </span> <span class="err">500  4710ms  &lt;-</span></span></div>
      <div class="ln gap"></div>
      <div class="ln" data-state="analyzing"><span class="t">12:05:01.210</span><span class="k">analyzing</span><span>analyze_root_cause</span></div>
      <div class="ln"><span class="t">12:05:06.884</span><span class="k"></span><span>payment-service stalls on the processor;</span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span>gateway gives up at 5s, charge + order</span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span>still commit downstream.</span></div>
      <div class="ln gap"></div>
      <div class="ln"><span class="t">12:05:06.890</span><span class="k">plan</span><span>POST /refundPayment <span class="dim">{"payment_id":"pay_91c2"}</span></span></div>
      <div class="ln"><span class="t"></span><span class="k"></span><span>POST /cancelOrder   <span class="dim">{"order_id":"ord_4e10"}</span></span></div>
      <div class="ln" data-state="remediating"><span class="t">12:05:06.891</span><span class="k hot">remediating</span><span class="wait">waiting on a human, 30m timeout</span></div>
      <div class="ln gap"></div>
      <div class="ln"><span class="t">12:07:42.019</span><span class="k">signal</span><span>approve_rollback</span></div>
      <div class="ln" data-state="compensating"><span class="t">12:07:42.025</span><span class="k">compensating</span><span>2 activities, in parallel</span></div>
      <div class="ln"><span class="t">12:07:42.311</span><span class="k"></span><span>/refundPayment 200  /cancelOrder 200</span></div>
      <div class="ln" data-state="resolved"><span class="t">12:07:42.312</span><span class="k good">resolved</span><span>2 compensating transactions applied</span></div>
    </div>
  </div>
</div>

<section id="lifecycle">
  <div class="sec-head">
    <div>
      <div class="num">01 / LIFECYCLE</div>
      <h2>One workflow, five states.</h2>
    </div>
    <p>
      Each incident is a single Temporal workflow. It moves through the same states you see on the
      dashboard, and every step is recorded in its event history, so a crash resumes from the last
      completed step instead of starting over.
    </p>
  </div>
  <div class="states">
    <div class="state-col">
      <h3>detecting</h3>
      <p>Fetches spans for the alert&rsquo;s trace IDs through the MCP tool <code>get_telemetry_context</code>.</p>
      <div class="fact">30s timeout, 3 attempts</div>
    </div>
    <div class="state-col">
      <h3>analyzing</h3>
      <p>A model reads the spans and returns a root cause plus the list of compensations to run.</p>
      <div class="fact">heartbeat every 15s</div>
    </div>
    <div class="state-col hold">
      <h3>remediating</h3>
      <p>Nothing runs yet. An operator approves, rejects, or asks a question, which triggers a fresh analysis.</p>
      <div class="fact">fails closed after 30 min</div>
    </div>
    <div class="state-col">
      <h3>compensating</h3>
      <p>Each compensation is its own activity. They run in parallel and retry on their own.</p>
      <div class="fact">409 / 404 count as done</div>
    </div>
    <div class="state-col done">
      <h3>resolved</h3>
      <p>The root cause and the steps that were applied get written to the incident record.</p>
      <div class="fact">or <span style="color:var(--signal)">failed</span>, with the reason</div>
    </div>
  </div>
</section>

<section id="decisions">
  <div class="sec-head">
    <div>
      <div class="num">02 / DESIGN</div>
      <h2>Opinions it holds.</h2>
    </div>
    <p>
      Automated remediation is only useful if you can trust it at 3am. These constraints are what
      make that trust reasonable.
    </p>
  </div>
  <div class="decisions">
    <div class="decision">
      <h3>A human signs off on every change.</h3>
      <p>The agent can diagnose and propose on its own, but it can&rsquo;t mutate production state until someone sends <code>approve_rollback</code>. No answer within 30 minutes counts as a no.</p>
      <div class="src">workflows/incident_response.py</div>
    </div>
    <div class="decision">
      <h3>Undo with sagas, not locks.</h3>
      <p>No two-phase commit across services. Each forward step has a compensating call, like <code>/refundPayment</code> for a charge, and those calls are idempotent, so running one twice does no harm.</p>
      <div class="src">activities/saga.py</div>
    </div>
    <div class="decision">
      <h3>Workflows never touch the network.</h3>
      <p>Workflow code is deterministic, so Temporal can replay it. Every model call, HTTP request, and mutation lives in an activity with its own timeout and retry policy.</p>
      <div class="src">activities/</div>
    </div>
    <div class="decision">
      <h3>Tools are stateless HTTP.</h3>
      <p>The agent reaches infrastructure through an MCP server where every request describes itself fully. Workers can come and go without session state getting lost.</p>
      <div class="src">mcp/server.py</div>
    </div>
  </div>
</section>

<section id="run">
  <div class="sec-head">
    <div>
      <div class="num">03 / USE IT</div>
      <h2>Wire it to your alerts.</h2>
    </div>
    <p>
      <code>POST /incident</code> accepts PagerDuty v3 and Alertmanager webhooks as they are, and
      works out which format it got. Or trigger a seeded test incident from the dashboard.
    </p>
  </div>
  <div class="split">
    <div>
      <h3>Alertmanager</h3>
      <p>Add a webhook receiver pointing at the dashboard. The first firing alert starts a workflow.</p>
<pre class="code"><span class="c"># alertmanager.yml</span>
receivers:
  - name: aegis
    webhook_configs:
      - url: <span class="s">http://aegis:7080/incident</span>

<span class="c"># or by hand</span>
curl -X POST localhost:7080/incident \
  -H 'content-type: application/json' \
  -d <span class="s">'{"alerts":[{"labels":{"service":"payment-service"},
       "annotations":{"summary":"p99 &gt; 4s"}}]}'</span></pre>
    </div>
    <div>
      <h3>Run the stack</h3>
      <p>Python 3.11+ and Docker. One process per terminal.</p>
      <ol class="run">
        <li><div><code>pip install -r requirements.txt &amp;&amp; cp .env.example .env</code><span class="note">Set OPENAI_API_KEY for real root-cause analysis.</span></div></li>
        <li><div><code>docker run --rm -p 7233:7233 -p 8080:8080 temporalio/auto-setup</code></div></li>
        <li><div><code>python -m sre_swarm.mock_services.app</code><span class="note">Order and payment services on :9090.</span></div></li>
        <li><div><code>python -m sre_swarm.mcp.server</code><span class="note">Tool server on :8081.</span></div></li>
        <li><div><code>python -m sre_swarm.worker</code></div></li>
        <li><div><code>python -m sre_swarm.dashboard.app</code><span class="note">Open <a href="/dashboard">/dashboard</a> and trigger an incident.</span></div></li>
      </ol>
    </div>
  </div>
</section>

<footer>
  <span>AegisSRE is a proof of concept. Please don&rsquo;t point it at production yet.</span>
  <span><a href="https://github.com/soggy8/AegisSRE" target="_blank" rel="noopener">github.com/soggy8/AegisSRE</a></span>
</footer>

</div>

<script>
(function () {
  const lines = Array.from(document.querySelectorAll('#term .ln'));
  const stateEl = document.getElementById('term-state');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let timers = [];

  function setState(s) {
    stateEl.textContent = s;
    stateEl.style.color = s === 'remediating' ? '#f2c94c' : s === 'resolved' ? '#6fcf97' : '';
  }

  function play() {
    timers.forEach(clearTimeout);
    timers = [];
    lines.forEach(l => l.classList.remove('on'));
    setState('detecting');
    if (reduce) {
      lines.forEach(l => l.classList.add('on'));
      setState('resolved');
      return;
    }
    let t = 200;
    lines.forEach(l => {
      const s = l.dataset.state;
      const pause = s === 'remediating' ? 1400 : s === 'analyzing' ? 900 : l.classList.contains('gap') ? 60 : 180;
      timers.push(setTimeout(() => {
        l.classList.add('on');
        if (s) setState(s);
      }, t));
      t += pause;
    });
  }

  document.getElementById('replay').addEventListener('click', play);
  play();
})();
</script>
</body>
</html>
"""
