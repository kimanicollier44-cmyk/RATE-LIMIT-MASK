import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m

def test_registry_seven():
    assert set(m.CORE_PROVIDERS) == {"github_rest", "gemini_api", "muse_model_api",
                                     "google_flow", "openai_api", "claude_api", "claude_code"}
    assert len(m.PROVIDERS) == 157

def test_token_bucket_refill():
    c = m.Clock(); b = m.TokenBucket(2, 1.0, c)
    assert b.allow().allowed and b.allow().allowed and not b.allow().allowed
    c.advance(1); assert b.allow().allowed

def test_fixed_window_boundary():
    w = m.FixedWindow(2, 60, m.Clock())
    assert [w.allow().allowed for _ in range(3)] == [True, True, False]

def test_sliding_log():
    c = m.Clock(); s = m.SlidingWindowLog(2, 60, c)
    assert s.allow().allowed and s.allow().allowed and not s.allow().allowed
    c.advance(61); assert s.allow().allowed

def test_github_headers():
    st = m.parse_headers({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0",
                          "X-RateLimit-Reset": "1760000000", "X-RateLimit-Resource": "search"})
    assert st.provider == "github_rest" and st.remaining == 0 and st.resource == "search"

def test_anthropic_split_meters():
    sim = m.SyntheticProvider("claude_api", m.Clock())
    r = sim.call(tokens_in=10, tokens_out=9999)
    assert not r.allowed and r.metric_hit == "output_tokens_per_minute"

def test_gemini_first_hit():
    sim = m.SyntheticProvider("gemini_api", m.Clock())
    rs = [sim.call() for _ in range(6)]
    assert rs[5].allowed is False and rs[5].metric_hit == "requests_per_minute"

def test_flow_credits_daily_first():
    sim = m.SyntheticProvider("google_flow", m.Clock())
    for _ in range(5):
        assert sim.call(kind="lite").allowed
    assert sim.daily == 0

def test_claude_code_session():
    sim = m.SyntheticProvider("claude_code", m.Clock())
    rs = [sim.call() for _ in range(11)]
    assert rs[10].metric_hit == "five_hour_session_window"

def test_mask_waits_not_sends():
    mask = m.RuntimeMask(m.TokenBucket(1, 0.01, m.Clock()), m.Clock())
    assert mask.before_request().outcome == "SENT"
    assert mask.before_request().outcome == "WAIT"

def test_gates_hold_evasion():
    for a in ("rotate_keys_to_evade", "spoof_ip_or_fingerprint", "farm_accounts",
              "ignore_retry_after", "hammer_after_429", "automate_flow_ui"):
        assert m.gate(a)[0] == "HOLD"

def test_suggestions_20():
    assert len(m.SUGGESTIONS_20) == 20

def test_policy_export_is_jsonable():
    import json; json.dumps(m.export_policy("github_rest"))

def test_tier_math():
    assert m.tier_upgrade_math("openai_api", 0)["next_tier"] == "Build"
    assert m.tier_upgrade_math("claude_code")["note"].startswith("no self-serve")

def test_tracker_metrics():
    tr = m.UnifiedTracker()
    tr.record("github_rest", "rest", 200, latency_ms=50, bytes_in=500, bytes_out=1500,
              tokens_in=0, tokens_out=0)
    tr.record("github_rest", "rest", 403, latency_ms=20, error_kind="secondary")
    a = tr.analytics()
    assert a["global"]["calls"] == 2 and a["global"]["limited"] == 1
    assert a["global"]["bytes_total"] == 2000
    assert a["per_provider"]["github_rest"]["success_rate"] == 0.5
    assert len(tr.check_all()) == 157 and "github_rest" in tr.to_csv()

def test_council_hold_and_advice():
    c = m.Council(m.UnifiedTracker())
    d = c.decide("farm_accounts", stakes="deploy")
    assert d.verdict == "HOLD" and len(d.muse_advice) == 4
    assert d.argon_trace["model"] == "google/gemini-carbon-4-extended-pro-thinking"
    assert d.argon_trace["stages"]["MAP"]["live_calls"] == 0

def test_council_project_management():
    c = m.Council(m.UnifiedTracker())
    p = c.manage_project(["pace_inside_limits"])
    assert p["project_verdict"] == "PROCEED" and p["steps"] == 1

def test_registry_expansion():
    assert len(m.MORE_PROVIDERS) == 50
    assert all(k in m.PROVIDERS for k in m.CORE_PROVIDERS)
    assert all(p.snapshot for p in m.MORE_PROVIDERS.values())

def test_watch_registry():
    assert len(m.WATCH_SOURCES) == 157 * 3
    assert {w["kind"] for w in m.WATCH_SOURCES} == {"rules", "news", "updates"}

def test_local_models():
    ai = m.LocalAI(m.UnifiedTracker())
    b = ai.brief("github_rest")
    assert b["authority"].startswith("none") and len(b["models"]) == 4
    assert b["slm_summary"] and b["llm_briefing"]
    assert ai.llm.generate("rate limits", 10) == ai.llm.generate("rate limits", 10)
    good = {"error_rate": 0.0, "limit_rate": 0.0, "latency_ms_p95": 100, "success_rate": 1.0}
    bad = {"error_rate": 0.7, "limit_rate": 0.6, "latency_ms_p95": 4800, "success_rate": 0.3}
    assert ai.ml.classify(good)[0] == "ok" and ai.ml.classify(bad)[0] == "critical"
    assert 0.0 <= ai.nnl.risk(good) < ai.nnl.risk(bad) <= 1.0

def test_scout_cycle():
    s = m.ScoutSwarm(m.LocalAI(m.UnifiedTracker()))
    st = s.status()
    assert st["bots_virtual"] == 250_000 and st["live_calls"] == 0 and st["watches"] == 471
    finds = s.cycle(m.SYNTHETIC_WIRE)
    assert any(f["change"] and "300 RPD" in f["change"] for f in finds)
    assert all(f["impact"] in ("high", "medium", "low") and f["briefing"] for f in finds)
    assert m.gate("live_scout_fetch")[0] == "HOLD" and s.fetch_live("github_rest")[0] == "HOLD"

def test_next_100_providers():
    assert len(m.MORE_PROVIDERS_100) == 100
    for k in ("binance_api", "nasa_api", "okta_api", "docker_hub_api", "kubernetes_api"):
        assert k in m.PROVIDERS

def test_universal_mask_learns_unknown():
    um = m.UniversalMask()
    assert um.fit("totally_new_api") == "generic_learned"
    um.after("totally_new_api", 200, {"x-ratelimit-limit-requests": "50"})
    assert m.DYNAMIC_PROVIDERS["totally_new_api"]["limit_requests"] == 50.0

def test_honeypot_log_only():
    hp = m.honeypot_report()
    assert hp["listener_open"] is False and hp["access_granted"] is False
    assert hp["attempts"] == 12 and hp["top_threat"] == 100

def test_chamber_never_executes():
    ch = m.chamber_run()
    assert ch["chamber_module_verbatim"] is True
    assert ch["all_executed"] is False and ch["total_network_attempts"] == 0
    assert (ch["allowed"], ch["held"]) == (4, 3)

def test_autosave_and_reboot(tmp_path):
    rt = m.ResilientRuntime(m.AutoSave(str(tmp_path)))
    rt.tracker.record("github_rest", "rest", 200, latency_ms=10)
    assert rt.checkpoint()["saved"] is True
    assert rt.autosave.load()["tampered"] is False
    crashed = rt.run_guarded(lambda: 1 / 0)
    assert crashed["ok"] is False and crashed["recovery"]["rebooted"] is True
    atk = rt.reboot("honeypot_severe")
    assert atk["safe_mode"] is True and atk["quarantine"]

def test_tampered_checkpoint_rejected(tmp_path):
    a = m.AutoSave(str(tmp_path))
    a.save({"x": 1}); a.save({"x": 2})
    with open(a.path, "a") as fh:
        fh.write("evil")
    loaded = a.load()
    assert loaded["tampered"] is True and loaded["restored_from"] == "checkpoint.prev"
    assert loaded["state"] == {"x": 1}

def test_catalog_all():
    s = m.APICatalog().summary()
    assert s["by_status"]["published_modelled"] == 157
    assert s["by_status"]["catalogued_seed"] >= 100
    assert s["categories_covered"] == 51

def test_elite_team_balance_and_mission():
    t = m.EliteScoutTeam()
    assert t.shard_map()["balanced"] is True
    mission = t.run_mission()
    assert mission["proposed"] == 4 and len(mission["rejected"]) == 1
    assert mission["live_calls"] == 0

def test_auto_update_cycle(tmp_path):
    up = m.AutoUpdater(m.AutoSave(str(tmp_path)))
    res = up.apply_wire()
    assert res["applied"] == 1
    assert up.effective_snapshot("gemini_api")["free_2.5_flash"].endswith("300 RPD")
    rb = up.rollback_last()
    assert rb["rolled_back"] is True and rb["restored_value"].endswith("250 RPD")

def test_auto_update_scheduler():
    sched = m.AutoUpdateScheduler(m.AutoUpdater(), m.Clock(), interval_seconds=60)
    assert sched.tick([])["ran"] is True
    assert sched.tick([])["ran"] is False
    sched.clock.advance(120)
    assert sched.tick([])["ran"] is True
