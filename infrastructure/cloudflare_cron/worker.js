/**
 * Cloudflare Worker Edge Cron -> GitHub Actions `workflow_dispatch` Bridge ($0/month)
 * Repository: `jamietan2002/spy-daytrader` (SPY Intraday Day-Trading Bot)
 *
 * Connects directly to `jamietan2002/spy-daytrader` via Cloudflare Workers Git Integration
 * (Root directory: `infrastructure/cloudflare_cron`).
 *
 * Evaluates exact US Eastern (`America/New_York`) wall-clock time (auto-handling EDT <-> EST
 * Daylight Saving Time transitions) on a single 5-minute Cron Trigger (`*\/5 * * * *`):
 *   - Mon-Fri 09:20 ET -> Session Part 1: Open -> Midday (`action: "session", until_et: "12:58"`)
 *   - Mon-Fri 12:50 ET -> Session Part 2: Midday -> 15:55 ET Flatten (`action: "session", until_et: "16:05"`)
 *
 * Required Worker Secret:
 *   - `GITHUB_PAT`: Fine-grained GitHub Personal Access Token scoped to `jamietan2002/spy-daytrader`
 *     with `Actions: Read and write` permission.
 * Optional Worker Variables / Secrets:
 *   - `GITHUB_OWNER`: Defaults to `"jamietan2002"`.
 *   - `GITHUB_REPO`: Defaults to `"spy-daytrader"`.
 *   - `TRIGGER_SECRET`: Optional bearer secret enabling manual HTTP POST `/dispatch?job=<id>`.
 */

const JOBS = [
  {
    id: "spy_morning_session",
    tz: "America/New_York",
    weekdays: ["Mon", "Tue", "Wed", "Thu", "Fri"],
    hour: 9,
    minute: 20,
    workflow: "intraday_session.yml",
    ref: "main",
    inputs: { action: "session", until_et: "12:58" },
  },
  {
    id: "spy_afternoon_session",
    tz: "America/New_York",
    weekdays: ["Mon", "Tue", "Wed", "Thu", "Fri"],
    hour: 12,
    minute: 50,
    workflow: "intraday_session.yml",
    ref: "main",
    inputs: { action: "session", until_et: "16:05" },
  },
];

function getWallClockParts(dateObj, timeZone) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    hour: "numeric",
    minute: "numeric",
    hour12: false,
  });
  const parts = {};
  for (const p of fmt.formatToParts(dateObj)) {
    parts[p.type] = p.value;
  }
  const hourNum = Number(parts.hour) % 24;
  const minBucket = Math.floor(Number(parts.minute) / 5) * 5;
  return {
    weekday: parts.weekday,
    hour: hourNum,
    minuteBucket: minBucket,
  };
}

async function dispatchWorkflow(env, job) {
  const owner = env.GITHUB_OWNER || "jamietan2002";
  const repo = env.GITHUB_REPO || "spy-daytrader";
  const pat = (env.GITHUB_PAT || "").trim();
  if (!pat) {
    throw new Error("Missing GITHUB_PAT secret in Cloudflare Worker environment.");
  }
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${job.workflow}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${pat}`,
      "Content-Type": "application/json",
      "User-Agent": "spy-daytrader-cloudflare-cron-dispatcher/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: job.ref,
      inputs: job.inputs,
    }),
  });
  if (!resp.ok) {
    const errBody = await resp.text();
    throw new Error(`GitHub dispatch failed (${resp.status}) for ${job.id}: ${errBody}`);
  }
  return { job_id: job.id, repo: `${owner}/${repo}`, workflow: job.workflow, status: resp.status };
}

export default {
  async scheduled(event, env, _ctx) {
    const scheduledDate = new Date(event.scheduledTime || Date.now());
    const matched = JOBS.filter((job) => {
      const clock = getWallClockParts(scheduledDate, job.tz);
      return (
        job.weekdays.includes(clock.weekday) &&
        clock.hour === job.hour &&
        clock.minuteBucket === job.minute
      );
    });

    if (matched.length === 0) {
      return;
    }

    const results = await Promise.allSettled(matched.map((job) => dispatchWorkflow(env, job)));
    for (const res of results) {
      if (res.status === "fulfilled") {
        console.log("[DISPATCHED]", JSON.stringify(res.value));
      } else {
        console.error("[DISPATCH_ERROR]", String(res.reason));
      }
    }
  },

  async fetch(request, env, _ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      const now = new Date();
      return Response.json({
        ok: true,
        service: "spy-daytrader-workflow-dispatcher",
        repo: `${env.GITHUB_OWNER || "jamietan2002"}/${env.GITHUB_REPO || "spy-daytrader"}`,
        et_clock: getWallClockParts(now, "America/New_York"),
        utc_clock: getWallClockParts(now, "UTC"),
        configured_jobs: JOBS.map((j) => ({
          id: j.id,
          workflow: j.workflow,
          tz: j.tz,
          weekdays: j.weekdays,
          time: `${String(j.hour).padStart(2, "0")}:${String(j.minute).padStart(2, "0")}`,
          inputs: j.inputs,
        })),
      });
    }

    if (request.method === "POST" && url.pathname === "/dispatch") {
      const expected = (env.TRIGGER_SECRET || "").trim();
      const auth = request.headers.get("Authorization") || "";
      if (!expected || auth !== `Bearer ${expected}`) {
        return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
      }
      const jobId = url.searchParams.get("job");
      const job = JOBS.find((j) => j.id === jobId);
      if (!job) {
        return Response.json({ ok: false, error: `Unknown job '${jobId}'` }, { status: 400 });
      }
      const out = await dispatchWorkflow(env, job);
      return Response.json({ ok: true, dispatched: out });
    }

    return Response.json({ ok: false, error: "Not Found" }, { status: 404 });
  },
};

