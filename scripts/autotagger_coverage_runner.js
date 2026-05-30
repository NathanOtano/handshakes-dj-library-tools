#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const AUDIO_EXT = new Set([".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".opus"]);

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      i += 1;
    }
  }
  return out;
}

function now() {
  return new Date().toISOString();
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function writeJson(file, obj) {
  fs.writeFileSync(file, JSON.stringify(obj, null, 2), "utf8");
}

function appendEvent(file, obj) {
  fs.appendFileSync(file, JSON.stringify({ ts: now(), ...obj }) + "\n", "utf8");
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function redact(obj) {
  const out = clone(obj);
  function walk(v) {
    if (!v || typeof v !== "object") return;
    for (const key of Object.keys(v)) {
      const lk = key.toLowerCase();
      if (lk.includes("secret") || lk.includes("password") || lk === "token" || lk === "clientid" || lk === "email") {
        v[key] = v[key] ? "<present>" : "";
      } else {
        walk(v[key]);
      }
    }
  }
  walk(out);
  return out;
}

function countM3u(file) {
  if (!file || !fs.existsSync(file)) return 0;
  return fs.readFileSync(file, "utf8").split(/\r?\n/).filter((line) => line.trim() && !line.startsWith("#")).length;
}

function countAudio(root) {
  if (!root || !fs.existsSync(root)) return { count: 0, errors: 1, kind: "missing" };
  const stat = fs.statSync(root);
  if (stat.isFile()) {
    if (root.toLowerCase().endsWith(".m3u") || root.toLowerCase().endsWith(".m3u8")) {
      return { count: countM3u(root), errors: 0, kind: "m3u" };
    }
    return { count: AUDIO_EXT.has(path.extname(root).toLowerCase()) ? 1 : 0, errors: 0, kind: "file" };
  }
  let count = 0;
  let errors = 0;
  const stack = [root];
  while (stack.length) {
    const cur = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(cur, { withFileTypes: true });
    } catch {
      errors += 1;
      continue;
    }
    for (const ent of entries) {
      const p = path.join(cur, ent.name);
      if (ent.isDirectory()) stack.push(p);
      else if (ent.isFile() && AUDIO_EXT.has(path.extname(ent.name).toLowerCase())) count += 1;
    }
  }
  return { count, errors, kind: "directory" };
}

function copyIfExists(src, destName, reportDir) {
  if (!src || !fs.existsSync(src)) return null;
  const dest = path.join(reportDir, destName);
  fs.copyFileSync(src, dest);
  return dest;
}

function boolValue(value, fallback) {
  if (value === undefined) return fallback;
  return String(value).toLowerCase() === "true";
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const wsUrl = args["ws-url"];
  const reportDir = args["report-dir"];
  const targetPath = args["target-path"];
  const serverPid = Number(args["server-pid"] || 0);
  const platforms = String(args.platforms || "spotify").split(",").map((item) => item.trim()).filter(Boolean);
  const tags = String(args.tags || "genre,style").split(",").map((item) => item.trim()).filter(Boolean);
  const threads = Number(args.threads || 2);
  const overwrite = boolValue(args.overwrite, false);
  const skipTagged = boolValue(args["skip-tagged"], false);

  if (!wsUrl || !reportDir || !targetPath) {
    throw new Error("Missing --ws-url, --report-dir or --target-path.");
  }
  ensureDir(reportDir);

  const settingsPath = path.join(process.env.APPDATA || "", "AutoTagger", "AutoTagger", "config", "settings.json");
  const runsDir = path.join(process.env.APPDATA || "", "AutoTagger", "AutoTagger", "config", "runs");
  const eventLog = path.join(reportDir, "AutoTagger-events.jsonl");
  const statusPath = path.join(reportDir, "live-status.json");
  const finalPath = path.join(reportDir, "final-summary.json");
  const mdPath = path.join(reportDir, "summary.md");

  const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  const profileName = settings.ui && settings.ui.autoTaggerProfile;
  const profiles = (settings.ui && settings.ui.autoTaggerProfiles) || [];
  const profile = profiles.find((p) => p.name === profileName) || profiles[0];
  if (!profile || !profile.config) throw new Error("AutoTagger active profile config not found.");

  const config = clone(profile.config);
  config.path = targetPath;
  config.platforms = platforms;
  config.tags = tags;
  config.stylesOptions = "mergeToGenres";
  config.mergeGenres = true;
  config.overwrite = overwrite;
  config.skipTagged = skipTagged;
  config.includeSubfolders = true;
  config.multiplatform = true;
  config.threads = threads;
  config.type = "autoTagger";

  const preflight = {
    runId: path.basename(reportDir),
    startedAt: now(),
    targetPath,
    serverPid,
    wsUrl,
    profileName,
    sourceProfilePath: profile.config.path,
    requestedPlatformsFromProfile: profile.config.platforms || [],
    chosenPlatforms: platforms,
    platformDecision: "Coverage retry: use only requested coverage platforms, preserve existing genres with overwrite=false by default.",
    tags: config.tags,
    stylesOptions: config.stylesOptions,
    mergeGenres: config.mergeGenres,
    overwrite: config.overwrite,
    skipTagged: config.skipTagged,
    strictness: config.strictness,
    threads: config.threads,
    targetAudioCount: countAudio(targetPath),
    settingsPath: "<AppData AutoTagger settings, not copied>",
    runsDir,
  };
  writeJson(path.join(reportDir, "preflight.json"), preflight);
  writeJson(path.join(reportDir, "start-packet-redacted.json"), {
    generatedAt: now(),
    action: "startTagging",
    target: targetPath,
    config: redact(config),
    playlist: null,
  });

  let total = null;
  let progress = 0;
  let done = false;
  const unique = new Set();
  const counts = { ok: 0, error: 0, skipped: 0, other: 0 };
  const platformCounts = {};
  const messages = {};
  let lastStatus = null;
  let lastWrite = 0;
  let startedTaggingAt = null;

  function statusObj(extra = {}) {
    return {
      generatedAt: now(),
      runId: path.basename(reportDir),
      targetPath,
      serverPid,
      wsUrl,
      platforms,
      total,
      progress,
      uniquePaths: unique.size,
      counts,
      platformCounts,
      topMessages: Object.fromEntries(Object.entries(messages).sort((a, b) => b[1] - a[1]).slice(0, 20)),
      lastStatus,
      startedTaggingAt,
      done,
      ...extra,
    };
  }

  function updateStatus(force = false, extra = {}) {
    const t = Date.now();
    if (!force && t - lastWrite < 2000) return;
    lastWrite = t;
    writeJson(statusPath, statusObj(extra));
  }

  appendEvent(eventLog, { action: "clientConnecting", wsUrl, targetPath, platforms });
  updateStatus(true, { state: "connecting" });
  const ws = new WebSocket(wsUrl);

  ws.addEventListener("open", () => {
    appendEvent(eventLog, { action: "clientConnected" });
    ws.send(JSON.stringify({ action: "startTagging", config, playlist: null }));
    appendEvent(eventLog, {
      action: "clientStartTagging",
      target: targetPath,
      platforms,
      tags: config.tags,
      overwrite: config.overwrite,
      mergeGenres: config.mergeGenres,
    });
    updateStatus(true, { state: "sent-startTagging" });
  });

  ws.addEventListener("message", (msg) => {
    let data;
    try {
      data = JSON.parse(msg.data.toString());
    } catch {
      data = { action: "raw", message: msg.data.toString() };
    }
    appendEvent(eventLog, data);
    if (data.action === "startTagging") {
      total = data.files ?? total;
      startedTaggingAt = now();
      updateStatus(true, { state: "tagging" });
      return;
    }
    if (data.action === "taggingProgress" && data.status) {
      progress = data.progress ?? progress;
      const outer = data.status;
      const inner = outer.status || {};
      const platform = outer.platform || "(unknown)";
      platformCounts[platform] ||= { ok: 0, error: 0, skipped: 0, other: 0 };
      lastStatus = outer;
      if (inner.path) unique.add(inner.path);
      const status = inner.status || "other";
      if (Object.prototype.hasOwnProperty.call(counts, status)) counts[status] += 1;
      else counts.other += 1;
      if (Object.prototype.hasOwnProperty.call(platformCounts[platform], status)) platformCounts[platform][status] += 1;
      else platformCounts[platform].other += 1;
      if (inner.message) messages[inner.message] = (messages[inner.message] || 0) + 1;
      updateStatus(false, { state: "tagging" });
      return;
    }
    if (data.action === "error") {
      if (data.message) messages[data.message] = (messages[data.message] || 0) + 1;
      updateStatus(true, { state: "error-event" });
      return;
    }
    if (data.action === "taggingDone") {
      done = true;
      const successSrc = data.data && data.data.successFile;
      const failedSrc = data.data && data.data.failedFile;
      const successCopy = copyIfExists(successSrc, "success.m3u", reportDir);
      const failedCopy = copyIfExists(failedSrc, "failed.m3u", reportDir);
      const final = statusObj({
        state: "done",
        finishedAt: now(),
        successFileSource: successSrc || null,
        failedFileSource: failedSrc || null,
        successFileCopy: successCopy,
        failedFileCopy: failedCopy,
        successM3uCount: countM3u(successCopy),
        failedM3uCount: countM3u(failedCopy),
        mutationWarning: "AutoTagger may write audio tags. This coverage runner used overwrite=false unless explicitly changed.",
      });
      writeJson(finalPath, final);
      fs.writeFileSync(mdPath, [
        "# AutoTagger coverage run summary",
        "",
        `- Run: ${path.basename(reportDir)}`,
        `- Target: ${targetPath}`,
        `- Platforms: ${platforms.join(", ")}`,
        `- Tags: ${config.tags.join(", ")}`,
        `- Overwrite: ${config.overwrite}`,
        `- Merge genres: ${config.mergeGenres}`,
        `- Started tagging: ${startedTaggingAt || ""}`,
        `- Finished: ${final.finishedAt}`,
        `- Total reported by AutoTagger: ${total}`,
        `- Unique paths seen: ${unique.size}`,
        `- OK events: ${counts.ok}`,
        `- Error events: ${counts.error}`,
        `- Success M3U entries: ${final.successM3uCount}`,
        `- Failed M3U entries: ${final.failedM3uCount}`,
        "",
        "Warning: AutoTagger writes directly to audio tags. This run keeps overwrite=false by default.",
      ].join("\n"), "utf8");
      updateStatus(true, { state: "done" });
      try { ws.close(); } catch {}
      setTimeout(() => process.exit(0), 500);
    }
  });

  ws.addEventListener("error", (err) => {
    appendEvent(eventLog, { action: "websocketError", message: err && err.message ? err.message : String(err) });
    updateStatus(true, { state: "websocket-error" });
  });

  ws.addEventListener("close", () => {
    appendEvent(eventLog, { action: "clientClosed", done });
    if (!done) {
      writeJson(finalPath, statusObj({ state: "closed-before-done", finishedAt: now() }));
      process.exit(2);
    }
  });

  setInterval(() => updateStatus(true, { state: done ? "done" : "tagging" }), 30000);
}

try {
  main();
} catch (err) {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}
