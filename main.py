#!/usr/bin/env python3
"""
RATE-LIMIT-MASK — educational reverse-engineering lab for API rate limits.

One runtime "mask" in front of fifty-seven limit systems (seven
deep-modelled, fifty registry companions added 2026-11-13):
  GitHub REST/GraphQL/Search, Gemini API, Meta Model API (Muse),
  Google Flow (credits, not an API), OpenAI API (ChatGPT models),
  Anthropic Claude API, and Claude Code (subscription windows).

The mask does NOT bypass anything. It studies how each system counts,
parses what each system reports, simulates each system offline with
synthetic fixtures, and — at runtime — paces a client so it stays
INSIDE the published limits: queue, honour Retry-After / reset,
back off, open a circuit breaker. Evasion actions are HOLD gates.

Pure Python stdlib. No live calls. No keys. All provider numbers are
a dated snapshot (checked 2026-11-13) — limits change; the registry
says so on every record and the parsers trust live headers over it.

Run:  python main.py           (full self-demo, CHECKS)
      python main.py providers (registry as JSON)
      python main.py simulate github_rest 6
"""
from __future__ import annotations

import json
import math
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from email.utils import parsedate_to_datetime

SNAPSHOT_DATE = "2026-11-13"

# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    kind: str                 # api | credit_app | subscription_agent
    scope: str                # what the budget is shared across
    metrics: tuple            # counting dimensions, first-hit wins
    headers: tuple            # header dialect it speaks
    on_limit: str             # what a limit response looks like
    reset_rule: str
    snapshot: tuple           # (label, value) headline numbers, dated
    notes: str

PROVIDERS = {
    "github_rest": Provider(
        key="github_rest", name="GitHub REST + GraphQL + Search", kind="api",
        scope="per user (PAT/OAuth) / per IP unauthenticated / per repo for GITHUB_TOKEN",
        metrics=("requests_per_hour", "graphql_points_per_hour", "secondary_points_per_minute",
                 "concurrent_requests", "content_creation_per_minute", "cpu_seconds_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used",
                 "x-ratelimit-reset", "x-ratelimit-resource", "retry-after"),
        on_limit="403 or 429, x-ratelimit-remaining: 0 (primary) or 'secondary rate limit' message",
        reset_rule="x-ratelimit-reset is UTC epoch seconds; secondary: honour retry-after, else >=60s backoff",
        snapshot=(("unauthenticated", "60 req/h per IP"),
                  ("authenticated", "5,000 req/h per user"),
                  ("enterprise_cloud_app", "15,000 req/h"),
                  ("actions_github_token", "1,000 req/h per repo"),
                  ("search_authenticated", "30 req/min"),
                  ("graphql", "5,000 points/h"),
                  ("secondary_points", "900 points/min per endpoint (GET=1pt, mutation=5pt)"),
                  ("secondary_concurrency", "100 concurrent"),
                  ("secondary_content", "80/min and 500/h content-creating")),
        notes="Conditional requests: an authenticated 304 does NOT count against the primary limit. /rate_limit itself is free.",
    ),
    "gemini_api": Provider(
        key="gemini_api", name="Google Gemini API (AI Studio)", kind="api",
        scope="per project (not per key), tier set by billing account",
        metrics=("requests_per_minute", "tokens_per_minute", "requests_per_day"),
        headers=(),  # quota is not reported per-response; error body carries it
        on_limit="429 with error status RESOURCE_EXHAUSTED; whichever of RPM/TPM/RPD hits first",
        reset_rule="RPM/TPM are rolling 60s windows; RPD resets midnight Pacific Time",
        snapshot=(("free_2.5_pro", "5 RPM / 250,000 TPM / 100 RPD"),
                  ("free_2.5_flash", "10 RPM / 250,000 TPM / 250 RPD"),
                  ("free_2.5_flash_lite", "15 RPM / 250,000 TPM / 1,000 RPD"),
                  ("paid_tiers", "Tier 1/2/3 raise all three; RPD cap lifts on paid")),
        notes="Three independent meters, first-hit wins: 15 tiny requests can exhaust RPM with TPM almost untouched.",
    ),
    "muse_model_api": Provider(
        key="muse_model_api", name="Meta Model API (Muse Spark / Wonder / Ember)", kind="api",
        scope="per team, per model (all keys in a team share one budget)",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                 "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
                 "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens", "retry-after"),
        on_limit="429 Too Many Requests with Retry-After",
        reset_rule="per-minute windows; reset headers report the window edge",
        snapshot=(("spark_standard", "3,000 RPM / 4,000,000 TPM"),
                  ("wonder", "3,000 RPM / 4,000,000 TPM"),
                  ("ember", "3,000 RPM / 4,000,000 TPM"),
                  ("spark_contributor", "150 RPM / 3,000,000 TPM"),
                  ("muse_image", "150 RPM, no TPM"),
                  ("background_responses", "600 submissions/min per team"),
                  ("voice_transcribe", "256 concurrent streams / 16,000 streams per hour")),
        notes="No tiers to climb: fixed per-team limits. Contributor is cheaper and slower, and its content may be used for training.",
    ),
    "google_flow": Provider(
        key="google_flow", name="Google Flow (Veo / Omni in Labs)", kind="credit_app",
        scope="per Google account: a daily credit grant plus a monthly plan pool",
        metrics=("daily_credits", "monthly_credits"),
        headers=(),  # consumer app, no API headers
        on_limit="'You've reached your usage limit' in-app; a failed generation is not charged",
        reset_rule="daily 50 credits refresh on first generation of the day and do NOT roll over; monthly pool resets per billing cycle",
        snapshot=(("every_account_daily", "50 credits/day"),
                  ("plus_monthly", "200 credits/mo"),
                  ("pro_monthly", "1,000 credits/mo"),
                  ("ultra_monthly", "10,000 or 25,000 credits/mo"),
                  ("veo_lite_cost", "10 credits (5 on Ultra)"),
                  ("veo_fast_cost", "20 credits (10 on Ultra)"),
                  ("veo_quality_cost", "100 credits")),
        notes="NOT an API rate limit: a credit ledger. Reverse-engineering it means modelling cost-per-generation against two buckets, daily first.",
    ),
    "openai_api": Provider(
        key="openai_api", name="OpenAI API (ChatGPT models)", kind="api",
        scope="per organization AND per project, per model (not per user/key)",
        metrics=("requests_per_minute", "tokens_per_minute", "requests_per_day",
                 "tokens_per_day", "images_per_minute"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                 "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
                 "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
        on_limit="429 rate_limit_exceeded; whichever metric hits first",
        reset_rule="per-minute windows recover continuously; daily meters reset per day",
        snapshot=(("build_flagship", "5,000 RPM / 1,000,000 TPM"),
                  ("launch_flagship", "10,000 RPM / 4,000,000 TPM"),
                  ("grow_flagship", "15,000 RPM / 40,000,000 TPM"),
                  ("grow_luna", "30,000 RPM / 180,000,000 TPM"),
                  ("tier_entry", "Build $5 / Launch $100 / Grow $500 total credit purchases")),
        notes="TPM counts cached input too (unlike Anthropic). Long-context requests on long-context models carry a separate limit.",
    ),
    "claude_api": Provider(
        key="claude_api", name="Anthropic Claude API", kind="api",
        scope="per organization, per model; usage tier set automatically",
        metrics=("requests_per_minute", "input_tokens_per_minute", "output_tokens_per_minute"),
        headers=("anthropic-ratelimit-requests-limit", "anthropic-ratelimit-requests-remaining",
                 "anthropic-ratelimit-requests-reset", "anthropic-ratelimit-input-tokens-limit",
                 "anthropic-ratelimit-input-tokens-remaining",
                 "anthropic-ratelimit-output-tokens-limit",
                 "anthropic-ratelimit-output-tokens-remaining", "retry-after"),
        on_limit="429 with retry-after; token-bucket refill means bursts can trip it below the average rate",
        reset_rule="limits refill continuously (token bucket), not at a fixed clock edge",
        snapshot=(("start_tier", "4,000 RPM / 2,000,000 ITPM / 400,000 OTPM (Opus/Sonnet class)"),
                  ("build_tier", "10,000 RPM / 5,000,000 ITPM / 1,000,000 OTPM"),
                  ("scale_tier", "13,000 RPM / 10,000,000 ITPM / 2,000,000 OTPM")),
        notes="Input and output tokens are metered SEPARATELY (ITPM vs OTPM) and cache reads do not count toward ITPM on current models.",
    ),
    "claude_code": Provider(
        key="claude_code", name="Claude Code (subscription)", kind="subscription_agent",
        scope="per subscription account, SHARED across Claude Code + Claude apps + Cowork",
        metrics=("five_hour_session_window", "weekly_window"),
        headers=(),  # no API headers; /usage inside the tool reports percentages
        on_limit="session limit message; window opens as the rolling 5h window slides",
        reset_rule="5-hour rolling window starts at first message; weekly cap is a rolling 7-day window on top",
        snapshot=(("pro", "~10-40 prompts per 5h window (Anthropic estimate)"),
                  ("max_5x", "~50-200 prompts per 5h window"),
                  ("max_20x", "~200-800 prompts per 5h window")),
        notes="A completely different regime from the Claude API: no RPM/TPM, no fixed prompt count — a long Opus session over a big repo burns the window far faster than short Haiku prompts. API keys do NOT use this regime.",
    ),
}

CORE_PROVIDERS = tuple(PROVIDERS)   # the original seven, deep-modelled

# --------------------------------------- 50 more APIs, same registry shape
# Added 2026-11-13 at the owner's order: fifty more limit systems similar
# to the original seven. Headline figures are a dated snapshot from public
# documentation (~ = approximate / plan-dependent); the same rule applies
# as everywhere in this lab — live headers and live docs beat the registry.

MORE_PROVIDERS = {
    "mistral_ai": Provider(
        key="mistral_ai", name="Mistral AI (La Plateforme)", kind="api",
        scope="per workspace / API key, tier set by spend",
        metrics=("requests_per_second", "tokens_per_minute", "tokens_per_month"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-second and per-minute windows; monthly token cap per billing cycle",
        snapshot=(("free_experiment", "~1 req/s class, token caps per month"),
                  ("paid", "limits scale with payment tier")),
        notes="Free Experiment plan is deliberately tiny; paid ceilings rise with cumulative spend."),
    "cohere": Provider(
        key="cohere", name="Cohere API", kind="api",
        scope="per API key; trial vs production keys",
        metrics=("requests_per_minute", "calls_per_month"),
        headers=("x-ratelimit-limit", "retry-after"),
        on_limit="429 Too Many Requests",
        reset_rule="per-minute windows; trial keys also carry a monthly call cap",
        snapshot=(("trial", "~20 RPM class, 1,000 calls/month"),
                  ("production", "higher RPM by plan")),
        notes="Trial keys are for evaluation only — production keys lift both meters."),
    "xai_grok": Provider(
        key="xai_grok", name="xAI Grok API", kind="api",
        scope="per team, per model",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens", "retry-after"),
        on_limit="429 with rate-limit headers",
        reset_rule="per-minute windows per model",
        snapshot=(("standard", "per-model RPM/TPM, tiered by team spend"),),
        notes="Same header dialect family as OpenAI/Muse; ceilings published per model."),
    "deepseek": Provider(
        key="deepseek", name="DeepSeek API", kind="api",
        scope="per account",
        metrics=("concurrent_requests",),
        headers=("retry-after",),
        on_limit="429 under load; no fixed published RPM",
        reset_rule="dynamic server-side throttling, no fixed window published",
        snapshot=(("chat_reasoner", "no fixed published RPM; concurrency throttled dynamically"),),
        notes="The honest outlier: DeepSeek publishes no hard RPM — the mask can only honour 429s and back off."),
    "groq": Provider(
        key="groq", name="Groq API", kind="api",
        scope="per organization, per model",
        metrics=("requests_per_minute", "tokens_per_minute", "requests_per_day", "tokens_per_day"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                 "x-ratelimit-limit-tokens", "x-ratelimit-reset-requests", "retry-after"),
        on_limit="429 rate_limit_exceeded with reset headers",
        reset_rule="per-minute and per-day windows per model",
        snapshot=(("free", "~30 RPM class; per-model daily caps (e.g. 1,000 RPD on 70B-class)"),
                  ("developer_paid", "higher per-model ceilings")),
        notes="Four meters per model — RPD is the one free-tier agents actually hit."),
    "together_ai": Provider(
        key="together_ai", name="Together AI API", kind="api",
        scope="per account, per model; tier set by payments",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-minute windows; tiers rise with billing history",
        snapshot=(("entry", "~60 RPM class to start"),
                  ("scaled", "ceilings rise with spend tiers")),
        notes="Serverless endpoints and dedicated endpoints meter differently."),
    "fireworks_ai": Provider(
        key="fireworks_ai", name="Fireworks AI API", kind="api",
        scope="per account, per deployment",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "retry-after"),
        on_limit="429 quota_exceeded",
        reset_rule="per-minute windows; quota raises are account-managed",
        snapshot=(("pay_as_you_go", "per-model RPM/TPM by account tier"),),
        notes="Default quotas are starting points; production ceilings are negotiated."),
    "perplexity": Provider(
        key="perplexity", name="Perplexity Sonar API", kind="api",
        scope="per account; tier set by cumulative spend",
        metrics=("requests_per_minute",),
        headers=("x-ratelimit-limit-requests", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-minute windows; tier ladder by total credits purchased",
        snapshot=(("tier_0", "low RPM to start"),
                  ("higher_tiers", "RPM climbs the spend ladder")),
        notes="A spend ladder like OpenAI's, but RPM is the headline meter."),
    "replicate": Provider(
        key="replicate", name="Replicate API", kind="api",
        scope="per account / API token",
        metrics=("requests_per_minute", "predictions_concurrent"),
        headers=("retry-after",),
        on_limit="429 throttled, especially before a payment method is on file",
        reset_rule="per-minute request windows; prediction creation throttles separately",
        snapshot=(("no_payment_method", "heavily throttled prediction creation"),
                  ("paid", "~hundreds of API requests/min class")),
        notes="Two different throttles: raw API calls vs prediction creation."),
    "huggingface": Provider(
        key="huggingface", name="Hugging Face Hub + Inference", kind="api",
        scope="per user / organization token",
        metrics=("requests_per_5_minutes", "inference_credits"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 with rate-limit headers (Hub); inference is credit-metered",
        reset_rule="rolling 5-minute windows on the Hub API",
        snapshot=(("free_hub", "~1,000 requests / 5 min class"),
                  ("pro_enterprise", "higher windows + more inference credits")),
        notes="Hub API and Inference Providers are two regimes sharing one token."),
    "azure_openai": Provider(
        key="azure_openai", name="Azure OpenAI", kind="api",
        scope="per deployment, per region, inside a subscription quota",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                 "azure-openai-requestslimit", "retry-after"),
        on_limit="429 Too Many Requests when deployment quota is spent",
        reset_rule="per-minute quota per deployment; quota is allocated, not global",
        snapshot=(("standard_deployment", "RPM/TPM set by the quota you assign the deployment"),),
        notes="The budget lives on the deployment, so one deployment starving says nothing about its neighbour."),
    "aws_bedrock": Provider(
        key="aws_bedrock", name="AWS Bedrock", kind="api",
        scope="per AWS account, per region, per model (service quotas)",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("retry-after",),
        on_limit="ThrottlingException / 429 ServiceQuotaExceeded",
        reset_rule="per-minute service quotas; increases via Service Quotas console",
        snapshot=(("default_quotas", "per-model RPM/TPM, account-level, region-scoped"),),
        notes="Quotas are per region — the same model is a different budget in us-east-1 and eu-west-1."),
    "vertex_ai": Provider(
        key="vertex_ai", name="Google Vertex AI", kind="api",
        scope="per project, per region, per model (shared quota pool)",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("retry-after",),
        on_limit="429 RESOURCE_EXHAUSTED when the shared quota pool is spent",
        reset_rule="per-minute quotas, shared across features using the same model",
        snapshot=(("gemini_on_vertex", "online-prediction QPM/TPM quotas per project+region"),),
        notes="Vertex and AI Studio meter the same Google models through different budgets."),
    "openrouter": Provider(
        key="openrouter", name="OpenRouter API", kind="api",
        scope="per account / key; free-model pool is separate",
        metrics=("requests_per_minute", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-minute window; free models also carry a daily cap",
        snapshot=(("free_models", "~20 RPM and ~50 RPD (daily cap rises after a credit purchase)"),
                  ("paid_models", "limited mainly by your credit balance")),
        notes="A router: upstream provider limits still apply underneath OpenRouter's own."),
    "elevenlabs": Provider(
        key="elevenlabs", name="ElevenLabs API", kind="api",
        scope="per account / plan",
        metrics=("concurrent_requests", "characters_per_month"),
        headers=("retry-after",),
        on_limit="429 concurrent-limit or quota (character) errors",
        reset_rule="concurrency is instant; character quota resets per billing cycle",
        snapshot=(("free", "low concurrency, small monthly character quota"),
                  ("paid_plans", "concurrency and characters scale by plan")),
        notes="A concurrency ceiling plus a monthly character ledger — two shapes at once."),
    "deepl": Provider(
        key="deepl", name="DeepL API", kind="api",
        scope="per API key / plan",
        metrics=("characters_per_month", "requests_per_minute"),
        headers=("retry-after",),
        on_limit="429 too many requests; 456 = monthly character quota exhausted",
        reset_rule="monthly character ledger; per-minute burst throttle on top",
        snapshot=(("free", "500,000 characters/month"),
                  ("pro", "character volume by plan, 456 when spent")),
        notes="DeepL has a dedicated quota status code (456) — most APIs fold it into 429."),
    "stability_ai": Provider(
        key="stability_ai", name="Stability AI API", kind="api",
        scope="per organization / account",
        metrics=("requests_per_10_seconds", "credits"),
        headers=("retry-after",),
        on_limit="429 rate limit; generations also spend credits",
        reset_rule="short burst window plus a credit balance that only refills by purchase/grant",
        snapshot=(("standard", "burst throttle per account; every generation priced in credits"),),
        notes="Like Flow, a credit ledger wearing an API's clothes."),
    "runway": Provider(
        key="runway", name="Runway API", kind="api",
        scope="per organization; tier set by usage",
        metrics=("requests_per_minute", "concurrent_generations", "credits"),
        headers=("retry-after",),
        on_limit="429 throttling on task creation",
        reset_rule="per-minute creation throttle; generations gated by concurrency + credits",
        snapshot=(("api_tiers", "task-creation RPM and concurrency scale by tier"),),
        notes="Video generation: the scarce resource is concurrent tasks, not raw requests."),
    "assemblyai": Provider(
        key="assemblyai", name="AssemblyAI API", kind="api",
        scope="per account / plan",
        metrics=("concurrent_transcriptions", "requests_per_minute"),
        headers=("retry-after",),
        on_limit="429 when the concurrency cap for the plan is reached",
        reset_rule="concurrency frees as jobs finish; request throttle is per-minute",
        snapshot=(("pay_as_you_go", "concurrency cap by plan; hours are the billed unit"),),
        notes="Async shape: submit cheap, concurrency is the wall."),
    "ai21": Provider(
        key="ai21", name="AI21 API", kind="api",
        scope="per account; trial vs paid",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("retry-after",),
        on_limit="429 rate limit exceeded",
        reset_rule="per-minute windows per endpoint family",
        snapshot=(("trial", "low RPM evaluation tier"),
                  ("paid", "higher RPM/TPM by plan")),
        notes="Endpoint families (chat vs task-specific) meter separately."),
    "voyage_ai": Provider(
        key="voyage_ai", name="Voyage AI API", kind="api",
        scope="per account / payment tier",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit-requests", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-minute windows; tiers unlock with a payment method and spend",
        snapshot=(("no_payment_method", "very low RPM/TPM evaluation limits"),
                  ("tiered_paid", "ceilings climb the payment tiers")),
        notes="Embeddings/rerank shop: the payment method itself is the tier gate."),
    "jina_ai": Provider(
        key="jina_ai", name="Jina AI (Reader / Embeddings)", kind="api",
        scope="per API key; keyless reader access is lower",
        metrics=("requests_per_minute", "tokens"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 rate limit; token balance gates paid endpoints",
        reset_rule="per-minute windows per endpoint",
        snapshot=(("with_key", "~hundreds of RPM class per endpoint"),
                  ("keyless_reader", "lower per-IP caps")),
        notes="Reader, embeddings and reranker share one key but not one meter."),
    "langsmith": Provider(
        key="langsmith", name="LangSmith API", kind="api",
        scope="per workspace / organization plan",
        metrics=("requests_per_minute", "traces_per_month"),
        headers=("x-ratelimit-limit", "retry-after"),
        on_limit="429 on API throttle; trace ingest capped by plan",
        reset_rule="per-minute API windows; monthly trace ledger per plan",
        snapshot=(("developer", "small monthly trace allowance"),
                  ("plus_enterprise", "trace volume and API ceilings by plan")),
        notes="Observability tooling with its own two-shape limit: ingest ledger + API throttle."),
    "pinecone": Provider(
        key="pinecone", name="Pinecone API", kind="api",
        scope="per project / index",
        metrics=("requests_per_minute", "requests_per_second"),
        headers=("retry-after",),
        on_limit="429 RESOURCE_EXHAUSTED per index or project",
        reset_rule="per-minute/per-second ceilings per index on serverless",
        snapshot=(("serverless", "per-index request ceilings; data-plane and control-plane split"),),
        notes="Control plane (manage indexes) and data plane (query/upsert) limit separately."),
}

MORE_PROVIDERS.update({
    "gitlab": Provider(
        key="gitlab", name="GitLab API", kind="api",
        scope="per user (authenticated) / per IP (unauthenticated)",
        metrics=("requests_per_minute",),
        headers=("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "retry-after"),
        on_limit="429 with RateLimit-* headers",
        reset_rule="per-minute windows; general authenticated traffic ~2,000 req/min per user",
        snapshot=(("authenticated_general", "~2,000 req/min per user"),
                  ("endpoint_specific", "search, package and CI endpoints carry tighter caps")),
        notes="GitLab publishes per-endpoint limit families on top of the general one."),
    "bitbucket": Provider(
        key="bitbucket", name="Bitbucket Cloud API", kind="api",
        scope="per user",
        metrics=("requests_per_hour",),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 Too Many Requests",
        reset_rule="hourly window per user",
        snapshot=(("api", "~1,000 requests/hour per user"),),
        notes="An hourly budget — the opposite shape from per-minute APIs."),
    "docker_hub": Provider(
        key="docker_hub", name="Docker Hub", kind="api",
        scope="per IP (anonymous pulls) / per account (authenticated)",
        metrics=("pulls_per_6_hours",),
        headers=("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "retry-after"),
        on_limit="429 on pulls past the window cap: 'toomanyrequests'",
        reset_rule="rolling 6-hour pull window",
        snapshot=(("anonymous", "100 pulls / 6 h per IP"),
                  ("free_authenticated", "200 pulls / 6 h per account"),
                  ("pro_team_business", "no pull cap")),
        notes="The limit that bites CI fleets: it counts image pulls, not API calls."),
    "npm_registry": Provider(
        key="npm_registry", name="npm Registry API", kind="api",
        scope="per IP / per token",
        metrics=("requests_per_minute",),
        headers=("retry-after",),
        on_limit="429 / 403 under abuse patterns; no published hard cap",
        reset_rule="no public fixed window; throttles are abuse-driven",
        snapshot=(("registry", "no published hard cap; generous by design, abuse throttles apply"),),
        notes="A registry, not a metered API — limits exist but are not a product surface."),
    "pypi": Provider(
        key="pypi", name="PyPI (Warehouse) API", kind="api",
        scope="per IP / per project",
        metrics=("requests_per_minute",),
        headers=("retry-after",),
        on_limit="429 under abuse; XML-RPC methods carry their own restrictions",
        reset_rule="no public fixed window; abuse-driven throttling",
        snapshot=(("warehouse", "no published hard cap; large-mirror and scrape traffic is throttled"),),
        notes="Same family as npm: the cap is real but unpublished — honour 429s."),
    "slack": Provider(
        key="slack", name="Slack Web API", kind="api",
        scope="per method tier, per workspace / app",
        metrics=("requests_per_minute",),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 rate_limited with Retry-After",
        reset_rule="per-minute windows by method tier",
        snapshot=(("tier_1", "~1 req/min"), ("tier_2", "~20 req/min"),
                  ("tier_3", "~50 req/min"), ("tier_4", "~100 req/min")),
        notes="The cleanest tier system in this registry: every method is labelled Tier 1-4 (or Special)."),
    "discord": Provider(
        key="discord", name="Discord API", kind="api",
        scope="global per bot token + per route buckets",
        metrics=("requests_per_second", "invalid_requests_per_10_minutes"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                 "x-ratelimit-reset-after", "x-ratelimit-bucket", "retry-after"),
        on_limit="429 with a bucket hash and reset-after; global 429 when the fleet cap trips",
        reset_rule="per-route sliding buckets + a 50 req/s global ceiling",
        snapshot=(("global", "50 requests/second per token"),
                  ("invalid_request_cap", "~10,000 invalid requests / 10 min triggers a block")),
        notes="Discord tells you the bucket and the exact reset — the best-mannered 429 here."),
    "telegram": Provider(
        key="telegram", name="Telegram Bot API", kind="api",
        scope="per bot, per chat, per group",
        metrics=("messages_per_second", "messages_per_chat_per_second", "messages_per_group_per_minute"),
        headers=("retry-after",),
        on_limit="429 with parameters.retry_after",
        reset_rule="~30 msg/s overall per bot; 1 msg/s per chat; ~20/min per group",
        snapshot=(("broadcast", "~30 messages/second per bot"),
                  ("single_chat", "1 message/second"), ("group", "~20 messages/minute")),
        notes="Three nested scopes at once — bot-wide, per-chat, per-group."),
    "twilio": Provider(
        key="twilio", name="Twilio API", kind="api",
        scope="per account (calls-per-second for voice; API concurrency)",
        metrics=("requests_per_second", "calls_per_second"),
        headers=("retry-after",),
        on_limit="429 error 20429 under concurrency/CPS pressure",
        reset_rule="short windows; voice CPS is an account setting, not a window",
        snapshot=(("voice_cps_default", "1 call/second until raised"),
                  ("api", "concurrency ceilings per account")),
        notes="Telephony shape: calls-per-second is provisioned, not windowed."),
    "sendgrid": Provider(
        key="sendgrid", name="SendGrid API", kind="api",
        scope="per account / API key",
        metrics=("requests_per_minute",),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 too many requests",
        reset_rule="per-minute windows (v3 API ~hundreds of requests/min class)",
        snapshot=(("v3_api", "~600 requests/min class for mail send; other endpoints lower"),),
        notes="Mail send is the generous endpoint; stats and suppression endpoints are tighter."),
    "stripe": Provider(
        key="stripe", name="Stripe API", kind="api",
        scope="per account, live vs test mode",
        metrics=("requests_per_second",),
        headers=("stripe-ratelimit-limit", "retry-after"),
        on_limit="429 rate_limit_error",
        reset_rule="per-second ceilings: ~100 req/s live mode, ~25 req/s test mode",
        snapshot=(("live_mode", "~100 requests/second"),
                  ("test_mode", "~25 requests/second")),
        notes="Payments: the limit you feel is usually your own webhook/DB, not Stripe's."),
    "reddit": Provider(
        key="reddit", name="Reddit API", kind="api",
        scope="per OAuth client id",
        metrics=("queries_per_minute",),
        headers=("x-ratelimit-used", "x-ratelimit-remaining", "x-ratelimit-reset"),
        on_limit="429 when the averaged window is spent",
        reset_rule="100 queries/minute averaged over a 10-minute window per OAuth client",
        snapshot=(("oauth", "~100 QPM averaged (10-min smoothing window)"),
                  ("unauthenticated", "much lower, per IP")),
        notes="Reddit averages: bursts are fine, sustained rate is what counts."),
    "x_api": Provider(
        key="x_api", name="X (Twitter) API", kind="api",
        scope="per app / per user token, per endpoint, 15-minute windows",
        metrics=("requests_per_15_minutes",),
        headers=("x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset"),
        on_limit="429 too many requests at the window edge",
        reset_rule="fixed 15-minute windows per endpoint; caps differ per product tier",
        snapshot=(("basic_pro", "per-endpoint caps per 15 min + monthly post caps by tier"),),
        notes="Two regimes stacked: 15-minute endpoint windows and monthly consumption caps."),
    "spotify": Provider(
        key="spotify", name="Spotify Web API", kind="api",
        scope="per app (client id), rolling window",
        metrics=("requests_per_30_seconds",),
        headers=("retry-after",),
        on_limit="429 with Retry-After in seconds",
        reset_rule="rolling 30-second window; the exact ceiling is undisclosed",
        snapshot=(("web_api", "rolling 30 s window; cap undisclosed — honour Retry-After"),),
        notes="The ceiling is a secret; Retry-After is the whole contract."),
    "youtube": Provider(
        key="youtube", name="YouTube Data API", kind="api",
        scope="per Google Cloud project",
        metrics=("quota_units_per_day",),
        headers=("retry-after",),
        on_limit="403 quotaExceeded when the daily unit budget is spent",
        reset_rule="10,000 units/day default; units are per-method costs (search = 100)",
        snapshot=(("default_quota", "10,000 units/day"),
                  ("search_call", "100 units"), ("upload", "1,600 units")),
        notes="A cost-weighted daily ledger — one search costs 100 list calls."),
    "google_maps": Provider(
        key="google_maps", name="Google Maps Platform", kind="api",
        scope="per API key / project, per Maps API",
        metrics=("requests_per_minute", "requests_per_day"),
        headers=("retry-after",),
        on_limit="429 / OVER_QUERY_LIMIT status per API",
        reset_rule="per-API quotas (QPM/QPD) set in Cloud Console; billing-coupled",
        snapshot=(("per_api", "each Maps API carries its own QPM/QPD quota"),),
        notes="Not one Maps limit but ~40 small ones, one per API surface."),
    "cloudflare": Provider(
        key="cloudflare", name="Cloudflare API", kind="api",
        scope="per user / token",
        metrics=("requests_per_5_minutes",),
        headers=("retry-after",),
        on_limit="429 error 971 / rate-limit responses past the window",
        reset_rule="global API ceiling ~1,200 requests per 5 minutes per user",
        snapshot=(("global_api", "~1,200 requests / 5 min per user"),),
        notes="One generous global ceiling; individual products add their own underneath."),
    "vercel": Provider(
        key="vercel", name="Vercel API", kind="api",
        scope="per account / team, by plan",
        metrics=("requests_per_minute",),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"),
        on_limit="429 rate_limited with reset headers",
        reset_rule="per-minute windows; ceilings scale with plan",
        snapshot=(("api", "per-endpoint limits by plan, headers report the window"),),
        notes="Headers-first design: the API tells you its own budget on every response."),
    "supabase": Provider(
        key="supabase", name="Supabase APIs", kind="api",
        scope="per project (Auth, REST, Storage meter separately)",
        metrics=("requests_per_minute", "emails_per_hour"),
        headers=("retry-after",),
        on_limit="429 on Auth endpoints; per-plan project throttles",
        reset_rule="Auth email/SMS sends have hourly caps; API ceilings scale by plan",
        snapshot=(("auth_emails", "built-in email provider ~ a few sends/hour until custom SMTP"),
                  ("api", "per-plan request ceilings per project")),
        notes="The limit people actually meet is the Auth email cap, not the data API."),
    "notion": Provider(
        key="notion", name="Notion API", kind="api",
        scope="per integration / workspace",
        metrics=("requests_per_second",),
        headers=("retry-after",),
        on_limit="429 rate_limited",
        reset_rule="average 3 requests/second per integration, bursts tolerated briefly",
        snapshot=(("integrations", "~3 requests/second average"),),
        notes="An average-rate limit: short bursts pass, sustained hammering does not."),
    "airtable": Provider(
        key="airtable", name="Airtable API", kind="api",
        scope="per base",
        metrics=("requests_per_second",),
        headers=("retry-after",),
        on_limit="429 RATE_LIMIT_REACHED (30 s cool-off after a breach)",
        reset_rule="5 requests/second per base; breaching triggers a 30-second block",
        snapshot=(("per_base", "5 requests/second"),
                  ("breach_penalty", "30-second block")),
        notes="One of the few APIs that punishes the breach itself with a timeout box."),
    "linear": Provider(
        key="linear", name="Linear API (GraphQL)", kind="api",
        scope="per user / API key",
        metrics=("requests_per_hour", "complexity_points_per_hour"),
        headers=("x-ratelimit-requests-limit", "x-ratelimit-requests-remaining",
                 "x-ratelimit-complexity-limit", "retry-after"),
        on_limit="429 / GraphQL RATE_LIMITED errors",
        reset_rule="hourly windows on both raw requests and query complexity",
        snapshot=(("requests", "~3,000 requests/hour per user"),
                  ("complexity", "hourly complexity-point budget on top")),
        notes="GraphQL done right: a cheap query and a monster query cost different amounts."),
    "twitch": Provider(
        key="twitch", name="Twitch Helix API", kind="api",
        scope="per client id (app token bucket)",
        metrics=("points_per_minute",),
        headers=("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "retry-after"),
        on_limit="429 when the point bucket empties",
        reset_rule="~800 points/minute bucket; endpoints cost 1+ points each",
        snapshot=(("app_access", "~800 points/minute"),
                  ("endpoint_cost", "most endpoints 1 point; some cost more")),
        notes="A token bucket where requests have prices — very close to GitHub GraphQL points."),
    "shopify": Provider(
        key="shopify", name="Shopify APIs", kind="api",
        scope="per store, per app",
        metrics=("requests_per_second", "graphql_cost_points"),
        headers=("x-shopify-shop-api-call-limit", "retry-after"),
        on_limit="429 / GraphQL THROTTLED when the cost budget is spent",
        reset_rule="REST: leaky bucket 40, refills 2/s (Plus: 4/s). GraphQL: cost points refill per second",
        snapshot=(("rest", "bucket 40, ~2 requests/second sustained"),
                  ("graphql", "calculated-cost budget, refills continuously")),
        notes="Two engines again: REST counts calls, GraphQL counts calculated cost."),
    "coinbase": Provider(
        key="coinbase", name="Coinbase Exchange API", kind="api",
        scope="per profile / per IP",
        metrics=("requests_per_second", "requests_per_hour"),
        headers=("retry-after",),
        on_limit="429 rate limit exceeded",
        reset_rule="per-second burst caps plus hourly ceilings (~10,000 req/h class per profile)",
        snapshot=(("public", "per-IP burst caps on market data"),
                  ("private", "~10,000 requests/hour class per profile")),
        notes="Public market-data and private trading traffic are metered on different scopes."),
    "openweather": Provider(
        key="openweather", name="OpenWeather API", kind="api",
        scope="per API key / plan",
        metrics=("calls_per_minute", "calls_per_month"),
        headers=("retry-after",),
        on_limit="429 when the per-minute cap trips; monthly cap blocks until reset/purchase",
        reset_rule="per-minute window + monthly ledger by plan",
        snapshot=(("free", "60 calls/minute, ~1,000,000 calls/month"),
                  ("paid", "per-minute and monthly caps scale by plan")),
        notes="Weather data: the monthly ledger is the real budget, the minute cap is just burst control."),
})

PROVIDERS.update(MORE_PROVIDERS)

# --------------------------------------- 100 more APIs (published-limit set)
# Added 2026-11-13 at the owner's order: the next 100 limit systems that
# PUBLISH a rate limit. Same rule: snapshot, hedged ~, live docs win.

MORE_PROVIDERS_100 = {
    "cerebras": Provider(
        key="cerebras", name="Cerebras Inference API", kind="api",
        scope="per organization, per model",
        metrics=("requests_per_minute", "tokens_per_minute", "requests_per_day", "tokens_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 rate_limit_exceeded",
        reset_rule="per-minute and per-day windows",
        snapshot=(("free", "~30 RPM class, per-model daily caps"),),
        notes="Very fast inference; free tier daily token caps are the binding meter."),
    "sambanova": Provider(
        key="sambanova", name="SambaNova Cloud API", kind="api",
        scope="per account, per model",
        metrics=("requests_per_minute", "tokens_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows by tier",
        snapshot=(("tiered", "limits published per model and tier (~)"),),
        notes="OpenAI-compatible surface; ceilings rise with plan tier."),
    "fal_ai": Provider(
        key="fal_ai", name="fal.ai API", kind="api",
        scope="per account",
        metrics=("concurrent_requests", "requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / concurrency rejection",
        reset_rule="concurrency window, queue-based",
        snapshot=(("queue", "concurrency-capped; requests queue instead of failing (~)"),),
        notes="Media generation: the meter is concurrent runs, not raw RPM."),
    "baseten": Provider(
        key="baseten", name="Baseten API", kind="api",
        scope="per workspace, per model",
        metrics=("concurrent_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / queue on concurrency cap",
        reset_rule="per-model concurrency window",
        snapshot=(("deployed_model", "concurrency limits set per deployment (~)"),),
        notes="Self-serve model hosting; autoscaling ceilings act as the limit."),
    "modal": Provider(
        key="modal", name="Modal API", kind="api",
        scope="per workspace",
        metrics=("concurrent_containers", "function_calls"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="resource limit errors at cap",
        reset_rule="concurrency quotas per workspace",
        snapshot=(("workspace", "container/GPU concurrency quotas by plan (~)"),),
        notes="Serverless compute: quotas are concurrency and GPU count, not RPM."),
    "datadog_api": Provider(
        key="datadog_api", name="Datadog API", kind="api",
        scope="per organization, per endpoint",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-endpoint hourly windows; headers name the period",
        snapshot=(("configured", "per-endpoint limits configured per org; headers report limit/period/remaining (~)"),),
        notes="One of the better-documented header dialects: limit, period, remaining, reset."),
    "sentry_api": Provider(
        key="sentry_api", name="Sentry API", kind="api",
        scope="per organization / auth token",
        metrics=("concurrent_requests", "requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-Sentry-Rate-Limits header",
        reset_rule="concurrent + per-minute windows",
        snapshot=(("org", "concurrent caps + per-minute rates, described in X-Sentry-Rate-Limits (~)"),),
        notes="Rate limit state is packed into a single structured header."),
    "new_relic_api": Provider(
        key="new_relic_api", name="New Relic APIs (NerdGraph / REST)", kind="api",
        scope="per account / key",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-minute windows",
        snapshot=(("account", "per-key RPM ceilings by API (~)"),),
        notes="NerdGraph and REST keys are metered separately."),
    "pagerduty_api": Provider(
        key="pagerduty_api", name="PagerDuty API", kind="api",
        scope="per account",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with ratelimit headers",
        reset_rule="per-minute window per account",
        snapshot=(("default", "~960 requests/minute per account class (~)"),),
        notes="Published default ceiling; abuse can lower it per account."),
    "grafana_api": Provider(
        key="grafana_api", name="Grafana / Grafana Cloud API", kind="api",
        scope="per instance / stack",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-second windows",
        snapshot=(("cloud", "per-stack request quotas by plan (~)"),),
        notes="Self-hosted instances set their own limits; Cloud publishes plan quotas."),
    "digitalocean_api": Provider(
        key="digitalocean_api", name="DigitalOcean API", kind="api",
        scope="per OAuth token / team",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with RateLimit headers",
        reset_rule="rolling hourly window, reset header in epoch",
        snapshot=(("oauth", "5,000 requests/hour, burst ~250 endpoints class (~)"),),
        notes="Classic hourly bucket with full RateLimit-* response headers."),
    "linode_api": Provider(
        key="linode_api", name="Linode API (Akamai Cloud)", kind="api",
        scope="per user, per endpoint class",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-endpoint-class windows (GET vs mutation differ)",
        snapshot=(("default", "per-endpoint rates published; mutations metered tighter (~)"),),
        notes="Different endpoints carry different per-minute ceilings."),
    "vultr_api": Provider(
        key="vultr_api", name="Vultr API", kind="api",
        scope="per API key",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-second window",
        snapshot=(("default", "~30 requests/second class (~)"),),
        notes="Simple per-second throttle per key."),
    "heroku_api": Provider(
        key="heroku_api", name="Heroku Platform API", kind="api",
        scope="per account",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with rate limit headers",
        reset_rule="rolling hourly window",
        snapshot=(("account", "4,500 calls/hour per account"),),
        notes="Token-bucket style hourly allowance, headers on every response."),
    "netlify_api": Provider(
        key="netlify_api", name="Netlify API", kind="api",
        scope="per user / site",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows per route class",
        snapshot=(("default", "per-route RPM ceilings published in docs (~)"),),
        notes="Deploy and site routes have separate ceilings."),
    "render_api": Provider(
        key="render_api", name="Render API", kind="api",
        scope="per workspace",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute window",
        snapshot=(("workspace", "~per-minute ceilings by plan (~)"),),
        notes="REST API throttled per workspace token."),
    "fly_io_api": Provider(
        key="fly_io_api", name="Fly.io API (Machines / GraphQL)", kind="api",
        scope="per organization",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / throttling errors",
        reset_rule="per-minute windows",
        snapshot=(("org", "GraphQL + Machines API throttles by org (~)"),),
        notes="Two surfaces, one org budget story."),
    "neon_api": Provider(
        key="neon_api", name="Neon API", kind="api",
        scope="per API key",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute window",
        snapshot=(("key", "~per-minute ceilings published (~)"),),
        notes="Database platform API; project operations metered per key."),
    "turso_api": Provider(
        key="turso_api", name="Turso Platform API", kind="api",
        scope="per API token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute window",
        snapshot=(("token", "per-token RPM ceilings (~)"),),
        notes="LibSQL platform management API."),
    "upstash_api": Provider(
        key="upstash_api", name="Upstash API (Redis / QStash)", kind="api",
        scope="per account / database",
        metrics=("requests_per_second", "daily_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / quota errors",
        reset_rule="per-second plus plan request quotas",
        snapshot=(("free_db", "per-database throughput caps by plan (~)"),),
        notes="Data-plane limits (commands/sec) matter more than management RPM."),
    "clerk_api": Provider(
        key="clerk_api", name="Clerk API", kind="api",
        scope="per instance",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with retry headers",
        reset_rule="per-minute windows per instance environment",
        snapshot=(("instance", "per-environment RPM ceilings (~)"),),
        notes="Dev instances are metered far tighter than production."),
    "auth0_api": Provider(
        key="auth0_api", name="Auth0 Management + Authentication APIs", kind="api",
        scope="per tenant",
        metrics=("requests_per_minute", "requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-tenant windows; burst + sustained pair",
        snapshot=(("tenant", "published per-endpoint burst/sustained pairs, plan-dependent (~)"),),
        notes="Auth0 publishes some of the most detailed per-endpoint limit tables in SaaS."),
    "okta_api": Provider(
        key="okta_api", name="Okta API", kind="api",
        scope="per organization, per endpoint family",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-Rate-Limit headers",
        reset_rule="per-minute windows per endpoint family, concurrent cap",
        snapshot=(("org", "per-family RPM tables published; concurrent request cap applies (~)"),),
        notes="The textbook enterprise case: every endpoint family has its own row."),
    "microsoft_graph": Provider(
        key="microsoft_graph", name="Microsoft Graph API", kind="api",
        scope="per app + tenant, per service",
        metrics=("requests_per_second", "requests_per_10s", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After",
        reset_rule="per-service throttling windows; Retry-After is authoritative",
        snapshot=(("app_tenant", "per-service limits (Outlook/Teams/etc each differ) (~)"),),
        notes="No single Graph limit: each workload service throttles independently."),
    "gcp_apis": Provider(
        key="gcp_apis", name="Google Cloud APIs (quota system)", kind="api",
        scope="per project, per API, per metric",
        metrics=("requests_per_minute", "quota_units"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / RESOURCE_EXHAUSTED quota errors",
        reset_rule="per-minute quota windows; daily quotas on some APIs",
        snapshot=(("project", "per-API quota pages; adjustable via Cloud Console quotas (~)"),),
        notes="Quota is a platform system: every API publishes its own metrics and limits."),
    "aws_quotas": Provider(
        key="aws_quotas", name="AWS APIs (service quotas + throttling)", kind="api",
        scope="per account + region, per service",
        metrics=("requests_per_second", "burst"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="ThrottlingException / 429 by service",
        reset_rule="token-bucket style per service+region; many quotas adjustable",
        snapshot=(("service_region", "per-service TPS + burst published in service docs (~)"),),
        notes="Throttling is per service, per region, per account — the original multi-dimensional case."),
    "cloudinary": Provider(
        key="cloudinary", name="Cloudinary API", kind="api",
        scope="per account (cloud)",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-FeatureRateLimit headers",
        reset_rule="hourly windows per feature class",
        snapshot=(("account", "hourly caps per feature (admin/upload/etc) by plan (~)"),),
        notes="Rate limit headers are split per feature, not one global bucket."),
    "bluesky_atproto": Provider(
        key="bluesky_atproto", name="Bluesky / AT Protocol API", kind="api",
        scope="per account / PDS",
        metrics=("requests_per_5_minutes", "points_per_hour", "points_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="points system: 3,000 points per 5 min class; per-endpoint costs",
        snapshot=(("account", "3,000 points/5min global class; createSession etc tighter (~)"),),
        notes="Points-based like GitHub GraphQL: endpoints cost different points."),
    "mastodon_api": Provider(
        key="mastodon_api", name="Mastodon API", kind="api",
        scope="per user / IP, per instance",
        metrics=("requests_per_5_minutes"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="5-minute windows (default instance config)",
        snapshot=(("default", "300 requests per 5 minutes (authenticated default) (~)"),),
        notes="Limits are per-instance configuration; defaults are widely copied."),
    "linkedin_api": Provider(
        key="linkedin_api", name="LinkedIn API", kind="api",
        scope="per app + member",
        metrics=("requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / throttling errors",
        reset_rule="daily windows per app+member+resource",
        snapshot=(("member_day", "per-resource daily quotas published per product (~)"),),
        notes="Daily application+member+resource triple-key metering."),
    "pinterest_api": Provider(
        key="pinterest_api", name="Pinterest API", kind="api",
        scope="per app + user",
        metrics=("requests_per_minute", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-minute + daily windows",
        snapshot=(("app_user", "per-endpoint RPM/daily pairs published (~)"),),
        notes="Standard and trial access tiers carry different ceilings."),
    "tiktok_api": Provider(
        key="tiktok_api", name="TikTok API (Research / Display / Marketing)", kind="api",
        scope="per app / advertiser",
        metrics=("requests_per_minute", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-endpoint windows by product",
        snapshot=(("app", "per-endpoint QPS/daily tables per product line (~)"),),
        notes="Each TikTok product API publishes its own throttling table."),
    "whatsapp_business": Provider(
        key="whatsapp_business", name="WhatsApp Business Platform (Meta)", kind="api",
        scope="per phone number / business",
        metrics=("messages_per_second", "api_calls_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / throughput errors",
        reset_rule="per-second messaging throughput tiers + hourly call caps",
        snapshot=(("number", "messaging throughput tiers 80-1,000 msg/s by quality; ~1M API calls/hour class per app (~)"),),
        notes="Two systems: Graph API call caps AND per-number message throughput by quality rating."),
    "messenger_platform": Provider(
        key="messenger_platform", name="Messenger Platform (Meta)", kind="api",
        scope="per page / app",
        metrics=("calls_per_second", "calls_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="613 / 429 throttling errors",
        reset_rule="per-second app-level + per-page windows",
        snapshot=(("page", "app-level calls/sec caps; Send API has per-page rates (~)"),),
        notes="Meta's older error-code throttling (code 613) predates plain 429s here."),
    "zoom_api": Provider(
        key="zoom_api", name="Zoom API", kind="api",
        scope="per account, by endpoint group",
        metrics=("requests_per_second", "daily_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-second rates by group (Light/Medium/Heavy/Resource-intensive) + daily caps",
        snapshot=(("account", "endpoint groups carry QPS from ~2 to ~80 by plan; daily caps on heavy groups (~)"),),
        notes="Zoom publishes limits by endpoint weight class — a model other APIs copy."),
    "intercom_api": Provider(
        key="intercom_api", name="Intercom API", kind="api",
        scope="per app (workspace)",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-minute window; headers report limit/remaining/reset",
        snapshot=(("app", "~1,000 requests/minute class per app (~)"),),
        notes="Clean header dialect, single per-app bucket."),
    "zendesk_api": Provider(
        key="zendesk_api", name="Zendesk API", kind="api",
        scope="per account",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After",
        reset_rule="per-minute windows; plan-scaled; some endpoints tighter",
        snapshot=(("account", "700 RPM (Growth) to 2,500 RPM (Enterprise) class; search endpoint tighter (~)"),),
        notes="Plan-tiered RPM is published in a public limits table."),
    "hubspot_api": Provider(
        key="hubspot_api", name="HubSpot API", kind="api",
        scope="per app / portal",
        metrics=("requests_per_second", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="10-second burst window + daily caps by tier",
        snapshot=(("standard", "100 requests/10s burst class; daily caps 250k-1M by tier (~)"),),
        notes="Burst is measured per 10 seconds, not per minute — window shape matters."),
    "salesforce_api": Provider(
        key="salesforce_api", name="Salesforce API", kind="api",
        scope="per org",
        metrics=("api_calls_per_24h", "concurrent_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="403 / 429 REQUEST_LIMIT_EXCEEDED",
        reset_rule="rolling 24-hour org allocation + concurrent long-request caps",
        snapshot=(("org", "per-edition 24h allocations (e.g. 100k+ by edition/licenses) (~)"),),
        notes="The daily-allocation model: one big 24h pool sized by licenses."),
    "mailchimp_api": Provider(
        key="mailchimp_api", name="Mailchimp API", kind="api",
        scope="per account",
        metrics=("concurrent_requests", "requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="concurrent connection cap + per-minute throttle",
        snapshot=(("account", "10 simultaneous connections class (~)"),),
        notes="Concurrency, not volume, is the documented choke point."),
    "resend_api": Provider(
        key="resend_api", name="Resend API", kind="api",
        scope="per team / API key",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with ratelimit headers",
        reset_rule="per-second window",
        snapshot=(("default", "~100 requests/second class team-wide (~)"),),
        notes="Small, honest, published single number."),
    "postmark_api": Provider(
        key="postmark_api", name="Postmark API", kind="api",
        scope="per account / server",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows per endpoint class",
        snapshot=(("account", "per-endpoint RPM ceilings published (~)"),),
        notes="Email API with per-endpoint published ceilings."),
    "vonage_api": Provider(
        key="vonage_api", name="Vonage APIs (Nexmo)", kind="api",
        scope="per account / application",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 throttled responses",
        reset_rule="per-second outbound rates per API",
        snapshot=(("account", "per-API req/s caps (SMS ~30/s class) (~)"),),
        notes="Outbound messaging is metered per second per destination country rules too."),
    "plivo_api": Provider(
        key="plivo_api", name="Plivo API", kind="api",
        scope="per account",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-second windows",
        snapshot=(("account", "per-account req/s ceilings (~)"),),
        notes="Telecom API; messaging + voice each metered."),
    "paypal_api": Provider(
        key="paypal_api", name="PayPal REST API", kind="api",
        scope="per client / account",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 RATE_LIMIT_REACHED",
        reset_rule="per-minute windows",
        snapshot=(("client", "per-endpoint RPM guidance published (~)"),),
        notes="429 body carries name RATE_LIMIT_REACHED."),
    "square_api": Provider(
        key="square_api", name="Square API", kind="api",
        scope="per location / application",
        metrics=("requests_per_endpoint_window"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 RATE_LIMITED",
        reset_rule="per-endpoint undisclosed windows; error code is the contract",
        snapshot=(("app", "limits not fully published; RATE_LIMITED error + backoff guidance (~)"),),
        notes="Documented as intentionally unpublished numbers: honour the error, back off."),
    "razorpay_api": Provider(
        key="razorpay_api", name="Razorpay API", kind="api",
        scope="per account (merchant)",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows",
        snapshot=(("merchant", "per-account RPM ceilings communicated per merchant (~)"),),
        notes="Payments in India; limits scale with merchant volume tier."),
    "kraken_api": Provider(
        key="kraken_api", name="Kraken API", kind="api",
        scope="per account",
        metrics=("points_per_window", "calls_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / EAPI:Rate limit exceeded",
        reset_rule="counter decays over time; call cost weights differ",
        snapshot=(("starter", "tiered counters: Starter 15 / Intermediate 20 / Pro 20+ with decay (~)"),),
        notes="Weighted call costs with a decaying counter — token bucket by another name."),
    "binance_api": Provider(
        key="binance_api", name="Binance API", kind="api",
        scope="per IP + per account (UID)",
        metrics=("request_weight_per_minute", "orders_per_second", "orders_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 then 418 (IP ban) on repeat abuse",
        reset_rule="per-minute weight window; order windows per second/day",
        snapshot=(("spot", "6,000 request weight/minute; order limits per symbol/account (~)"),),
        notes="The escalation case study: ignoring 429s earns a 418 IP ban."),
    "alpaca_api": Provider(
        key="alpaca_api", name="Alpaca API", kind="api",
        scope="per API key",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute window",
        snapshot=(("default", "200 requests/minute (non-commercial class)"),),
        notes="One flat published number for trading + data (paid data raises it)."),
    "polygon_api": Provider(
        key="polygon_api", name="Polygon.io API", kind="api",
        scope="per API key / plan",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute window",
        snapshot=(("free", "5 requests/minute free; paid tiers lift RPM (~)"),),
        notes="Free tier is deliberately tiny: 5/min is the famous number."),
    "alpha_vantage": Provider(
        key="alpha_vantage", name="Alpha Vantage API", kind="api",
        scope="per API key",
        metrics=("requests_per_day", "requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / throttle note in body",
        reset_rule="daily cap + per-minute burst",
        snapshot=(("free", "25 requests/day free (raised from 25; premium lifts both) (~)"),),
        notes="Daily-quota-first design; body contains a plain-English throttle note."),
    "twelvedata_api": Provider(
        key="twelvedata_api", name="Twelve Data API", kind="api",
        scope="per API key",
        metrics=("requests_per_minute", "credits_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute rate + daily credit ledger",
        snapshot=(("free", "8 RPM / 800 credits/day free class (~)"),),
        notes="Credits-per-symbol-call make cost per request variable."),
    "finnhub_api": Provider(
        key="finnhub_api", name="Finnhub API", kind="api",
        scope="per API key",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 API limit reached",
        reset_rule="per-minute window",
        snapshot=(("free", "60 calls/minute free class (~)"),),
        notes="Flat per-minute ceiling by plan."),
    "plaid_api": Provider(
        key="plaid_api", name="Plaid API", kind="api",
        scope="per client, per endpoint family",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 RATE_LIMIT_EXCEEDED",
        reset_rule="per-minute windows per endpoint family",
        snapshot=(("client", "per-endpoint-family RPM tables published (~)"),),
        notes="Error codes distinguish rate limits per product endpoint."),
    "quickbooks_api": Provider(
        key="quickbooks_api", name="QuickBooks Online API", kind="api",
        scope="per realm (company) + app",
        metrics=("requests_per_minute", "concurrent_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-minute + concurrency windows",
        snapshot=(("realm", "500 requests/minute per realm; 10 concurrent per realm class (~)"),),
        notes="Metered per company file (realm), not per user."),
    "xero_api": Provider(
        key="xero_api", name="Xero API", kind="api",
        scope="per tenant / app",
        metrics=("requests_per_minute", "requests_per_day", "concurrent_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-MinLimit headers",
        reset_rule="per-minute, daily and concurrent triple limits",
        snapshot=(("tenant", "60 RPM / 5,000 per day / 5 concurrent per tenant (~)"),),
        notes="Publishes minute, day AND concurrency in one table."),
    "etsy_api": Provider(
        key="etsy_api", name="Etsy Open API", kind="api",
        scope="per API key",
        metrics=("requests_per_second", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-second rate + daily quota",
        snapshot=(("keystring", "10 QPS / 10,000 QPD class per key (~)"),),
        notes="QPS+QPD pair is published in the developer docs."),
    "ebay_api": Provider(
        key="ebay_api", name="eBay APIs", kind="api",
        scope="per app + user, per API",
        metrics=("calls_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="error 429 / quota errors",
        reset_rule="daily call quotas per API, some hourly",
        snapshot=(("app", "per-API daily quotas published (e.g. Buy APIs 5,000/day class) (~)"),),
        notes="Quota-per-API model: every eBay API has its own daily table row."),
    "amazon_sp_api": Provider(
        key="amazon_sp_api", name="Amazon Selling Partner API", kind="api",
        scope="per seller + operation",
        metrics=("requests_per_second", "burst"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with x-amzn-RateLimit-Limit header",
        reset_rule="per-operation token-bucket (rate + burst published per operation)",
        snapshot=(("operation", "usage plans publish rate/burst per operation (~)"),),
        notes="The header literally returns your token-bucket rate for that operation."),
    "uber_api": Provider(
        key="uber_api", name="Uber API", kind="api",
        scope="per access token / app",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="hourly windows per endpoint",
        snapshot=(("app", "per-endpoint hourly ceilings published (~)"),),
        notes="Rides + Eats developer surfaces metered per endpoint."),
    "mapbox_api": Provider(
        key="mapbox_api", name="Mapbox APIs", kind="api",
        scope="per account / token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows per API",
        snapshot=(("account", "per-API RPM tables (geocoding 600/min class) (~)"),),
        notes="Each Mapbox API publishes its own per-minute row."),
    "tomtom_api": Provider(
        key="tomtom_api", name="TomTom APIs", kind="api",
        scope="per API key",
        metrics=("requests_per_second", "daily_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-second + daily caps by key tier",
        snapshot=(("key", "per-API QPS and daily transaction caps (~)"),),
        notes="Transactions, not requests, are the billing/limit unit."),
    "here_api": Provider(
        key="here_api", name="HERE APIs", kind="api",
        scope="per app / project",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="per-second windows",
        snapshot=(("app", "per-service req/s ceilings (~)"),),
        notes="Freemium plans cap monthly transactions too."),
    "foursquare_api": Provider(
        key="foursquare_api", name="Foursquare Places API", kind="api",
        scope="per project / API key",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="hourly windows",
        snapshot=(("project", "per-project hourly ceilings by tier (~)"),),
        notes="Places data metered per project."),
    "yelp_api": Provider(
        key="yelp_api", name="Yelp Fusion API", kind="api",
        scope="per API key",
        metrics=("calls_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 / quota errors",
        reset_rule="daily quota window",
        snapshot=(("business", "5,000 calls/day class (~)"),),
        notes="Daily quota is the whole story for the standard key."),
    "nasa_api": Provider(
        key="nasa_api", name="NASA APIs (api.nasa.gov)", kind="api",
        scope="per API key / IP",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="hourly windows",
        snapshot=(("registered", "1,000 requests/hour registered; DEMO_KEY 30/hour per IP"),),
        notes="The most-taught rate limit on the internet: DEMO_KEY vs registered key."),
    "news_api": Provider(
        key="news_api", name="NewsAPI", kind="api",
        scope="per API key",
        metrics=("requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 rateLimited error",
        reset_rule="daily window (developer plan)",
        snapshot=(("developer", "100 requests/day on the free developer plan"),),
        notes="Error body code 'rateLimited' is the contract."),
    "guardian_api": Provider(
        key="guardian_api", name="The Guardian Open Platform", kind="api",
        scope="per API key",
        metrics=("requests_per_second", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-second + daily windows",
        snapshot=(("developer", "~12 req/s and ~5,000/day class for developer keys (~)"),),
        notes="Content API with both burst and daily meters."),
    "nyt_api": Provider(
        key="nyt_api", name="NYT APIs", kind="api",
        scope="per API key",
        metrics=("requests_per_minute", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute + daily windows",
        snapshot=(("key", "10 requests/minute and 500/day per API (~)"),),
        notes="Small published numbers; per-API keys metered separately."),
    "stackexchange_api": Provider(
        key="stackexchange_api", name="Stack Exchange API", kind="api",
        scope="per IP / app key",
        metrics=("requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with backoff field in body",
        reset_rule="daily quota; body can carry an explicit backoff seconds field",
        snapshot=(("anonymous", "10,000/day with key (300/day IP-only class); backoff field must be honoured (~)"),),
        notes="Unique: the JSON body itself can order a backoff — violating it voids quota."),
    "wikimedia_api": Provider(
        key="wikimedia_api", name="Wikimedia / Wikipedia APIs", kind="api",
        scope="per client (User-Agent identified)",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After",
        reset_rule="politeness throttling; identified clients get higher ceilings",
        snapshot=(("identified", "published access policies; polite identified clients ~higher req/s (~)"),),
        notes="Identity here is a User-Agent string, not a key — honesty is the credential."),
    "openalex_api": Provider(
        key="openalex_api", name="OpenAlex API", kind="api",
        scope="per API key / polite pool",
        metrics=("requests_per_day", "requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="daily windows; mailto polite pool historically",
        snapshot=(("free", "usage-based pricing era: per-day request budgets by plan (~)"),),
        notes="Scholarly graph; single-record calls are cheap, list calls cost more."),
    "crossref_api": Provider(
        key="crossref_api", name="Crossref API", kind="api",
        scope="per client (polite pool)",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on throttle",
        reset_rule="politeness pools: public / polite / plus",
        snapshot=(("polite", "polite pool (with contact mailto) gets higher req/s than public (~)"),),
        notes="Three pools by behaviour, not by payment."),
    "noaa_api": Provider(
        key="noaa_api", name="NOAA / National Weather Service API", kind="api",
        scope="per client (User-Agent identified)",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-second guideline window",
        snapshot=(("guideline", "~5 requests/second guideline; unique User-Agent required (~)"),),
        notes="US national weather: a national digital product with a published politeness limit."),
    "monday_api": Provider(
        key="monday_api", name="monday.com API", kind="api",
        scope="per account / user token",
        metrics=("complexity_points_per_minute", "requests_per_minute", "concurrency"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 complexity / rate errors",
        reset_rule="per-minute complexity budget + concurrency cap",
        snapshot=(("account", "complexity points per minute budget; 25 concurrent class (~)"),),
        notes="Queries cost complexity points — GraphQL cost accounting like GitHub."),
    "asana_api": Provider(
        key="asana_api", name="Asana API", kind="api",
        scope="per bearer token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After",
        reset_rule="per-minute windows; distributor endpoints differ",
        snapshot=(("token", "1,500 requests/minute per token class (~)"),),
        notes="One generous flat number, published."),
    "clickup_api": Provider(
        key="clickup_api", name="ClickUp API", kind="api",
        scope="per personal token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute windows by plan",
        snapshot=(("token", "100 RPM (free class) scaling by plan (~)"),),
        notes="Plan-scaled per-token RPM."),
    "todoist_api": Provider(
        key="todoist_api", name="Todoist API", kind="api",
        scope="per user token",
        metrics=("requests_per_15_minutes"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="15-minute windows (REST vs Sync differ)",
        snapshot=(("sync_api", "450 requests per 15 min (Sync) / 1,000 per 15 min (REST) class (~)"),),
        notes="Unusual 15-minute window — window length is part of the fingerprint."),
    "dropbox_api": Provider(
        key="dropbox_api", name="Dropbox API", kind="api",
        scope="per app / team",
        metrics=("requests_per_window"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After (error too_many_requests)",
        reset_rule="undisclosed windows; Retry-After authoritative",
        snapshot=(("app", "numbers unpublished; 429 body + Retry-After are the contract (~)"),),
        notes="Deliberately unpublished: the mask must be header-driven here."),
    "box_api": Provider(
        key="box_api", name="Box API", kind="api",
        scope="per user / enterprise",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After",
        reset_rule="per-minute windows per API class",
        snapshot=(("user", "~1,000 RPM API class with per-endpoint variations (~)"),),
        notes="Per-user metering across an enterprise content cloud."),
    "google_drive_api": Provider(
        key="google_drive_api", name="Google Drive API", kind="api",
        scope="per project + per user",
        metrics=("queries_per_minute", "queries_per_minute_per_user"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="403/429 quota errors (rateLimitExceeded)",
        reset_rule="per-minute project + per-user pair",
        snapshot=(("project", "per-project and per-user per-minute quotas in Cloud Console (~)"),),
        notes="Two-level quota: project ceiling AND per-user ceiling inside it."),
    "figma_api": Provider(
        key="figma_api", name="Figma REST API", kind="api",
        scope="per access token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Retry-After + X-Figma-Rate-Limit-Type header",
        reset_rule="per-minute windows; tiered by plan + endpoint cost class",
        snapshot=(("starter", "published cost-class tiers (Tier 1 endpoints cheapest) by seat plan (~)"),),
        notes="Newer published model: endpoints grouped into rate tiers by cost."),
    "miro_api": Provider(
        key="miro_api", name="Miro API", kind="api",
        scope="per access token / team",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-minute windows",
        snapshot=(("token", "per-token RPM ceilings published (~)"),),
        notes="Whiteboard platform, standard header dialect."),
    "webflow_api": Provider(
        key="webflow_api", name="Webflow API", kind="api",
        scope="per site / workspace token",
        metrics=("requests_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-RateLimit headers",
        reset_rule="per-minute windows per token type",
        snapshot=(("site_token", "60 RPM (site) / 120 RPM (workspace) class (~)"),),
        notes="Token type changes the ceiling."),
    "contentful_api": Provider(
        key="contentful_api", name="Contentful APIs (CDA/CMA/CPA)", kind="api",
        scope="per space + environment",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with X-Contentful-RateLimit headers + reset seconds",
        reset_rule="per-second windows; header gives seconds-to-reset",
        snapshot=(("space", "delivery ~per-second ceilings by API type; headers name reset (~)"),),
        notes="Headers carry RateLimit-Reset as relative seconds — a dialect variant."),
    "sanity_api": Provider(
        key="sanity_api", name="Sanity API", kind="api",
        scope="per project",
        metrics=("requests_per_minute", "concurrent_requests"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="per-minute + concurrency windows",
        snapshot=(("project", "per-project API ceilings by plan (~)"),),
        notes="Content lake APIs metered per project."),
    "airtable_pat": Provider(
        key="airtable_pat", name="Airtable API (base-level)", kind="api",
        scope="per base",
        metrics=("requests_per_second"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with 30s block",
        reset_rule="per-second window per base; 429 blocks for 30 seconds",
        snapshot=(("base", "5 requests/second per base; exceed and the base blocks 30s"),),
        notes="Per-base (not per-key) metering with a fixed penalty box."),
    "twitch_helix": Provider(
        key="twitch_helix", name="Twitch Helix API (cost system)", kind="api",
        scope="per user + app, points bucket",
        metrics=("points_per_minute"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 with Ratelimit headers",
        reset_rule="points bucket refills per minute; endpoints cost 1+ points",
        snapshot=(("app_user", "800 points/minute (app+user bucket) class (~)"),),
        notes="Points bucket distinct from the earlier registry's channel-points entry: this is Helix API cost."),
    "discord_bot_gateway": Provider(
        key="discord_bot_gateway", name="Discord Gateway / interactions", kind="api",
        scope="per bot session",
        metrics=("events_per_60s", "identify_starts_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="gateway close codes / invalid session",
        reset_rule="per-session event windows + daily identify allowance",
        snapshot=(("bot", "120 gateway events per 60s per session; 1,000 identifies/day (scales with shards) (~)"),),
        notes="Not HTTP at all: websocket event budget + daily session-start quota."),
    "kubernetes_api": Provider(
        key="kubernetes_api", name="Kubernetes API (client-side + APF)", kind="api",
        scope="per client / API server",
        metrics=("requests_per_second", "priority_levels"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 from API Priority and Fairness",
        reset_rule="server-side flow control queues by priority level",
        snapshot=(("client", "client-go defaults QPS 5 / burst 10; APF queues server-side (~)"),),
        notes="National/international infra runs on this: rate limiting as fairness queueing."),
    "docker_hub_api": Provider(
        key="docker_hub_api", name="Docker Hub (registry pulls)", kind="api",
        scope="per IP / account",
        metrics=("pulls_per_6h"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on pull limit",
        reset_rule="rolling 6-hour pull windows",
        snapshot=(("anonymous", "100 pulls/6h anonymous IP; 200/6h free account; paid lifts cap (~)"),),
        notes="The limit Darion hits publishing: container pulls are rate limited too."),
    "gitlab_runner_ci": Provider(
        key="gitlab_runner_ci", name="GitHub Actions / GitLab CI (runner minutes)", kind="api",
        scope="per account / plan",
        metrics=("minutes_per_month"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="jobs queue / fail at quota",
        reset_rule="monthly minute ledger",
        snapshot=(("free_tier", "hosted runner minutes metered monthly by plan (~)"),),
        notes="CI time is a rate limit denominated in minutes, not requests."),
    "openstreetmap_api": Provider(
        key="openstreetmap_api", name="OpenStreetMap API / tile usage policy", kind="api",
        scope="per client (User-Agent identified)",
        metrics=("requests_per_second", "tiles_per_window"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="403/429 on policy breach, tile blocks",
        reset_rule="usage-policy windows; heavy use must use tile providers",
        snapshot=(("policy", "max ~1 req/s for API; tile usage policy caps and blocks abusers (~)"),),
        notes="A national/international commons: the limit is a published usage policy, enforced by blocks."),
    "brevo_api": Provider(
        key="brevo_api", name="Brevo (Sendinblue) API", kind="api",
        scope="per account / API key",
        metrics=("requests_per_hour"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="hourly windows",
        snapshot=(("account", "per-key hourly ceilings published (~)"),),
        notes="Email/SMS platform; transactional and marketing metered together."),
    "coinapi": Provider(
        key="coinapi", name="CoinAPI", kind="api",
        scope="per API key",
        metrics=("requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on quota",
        reset_rule="daily quota window",
        snapshot=(("free", "100 requests/day free class (~)"),),
        notes="Crypto market data priced in daily request quotas."),
    "weatherapi_com": Provider(
        key="weatherapi_com", name="WeatherAPI.com", kind="api",
        scope="per API key",
        metrics=("calls_per_month"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="403/429 on quota",
        reset_rule="monthly call ledger",
        snapshot=(("free", "1,000,000 calls/month free class (~)"),),
        notes="Weather at scale: monthly ledger, burst rarely binding."),
    "tomorrow_io": Provider(
        key="tomorrow_io", name="Tomorrow.io Weather API", kind="api",
        scope="per API key",
        metrics=("requests_per_hour", "requests_per_day"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on limit",
        reset_rule="hourly + daily windows",
        snapshot=(("free", "500/hour and 1,000/day class free (~)"),),
        notes="Hourly and daily pair, both published."),
    "ipinfo_api": Provider(
        key="ipinfo_api", name="IPinfo API", kind="api",
        scope="per access token",
        metrics=("requests_per_month"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="429 on quota",
        reset_rule="monthly quota window",
        snapshot=(("free", "50,000 requests/month free class (~)"),),
        notes="IP data metered as a monthly allowance."),
    "exchangerate_host": Provider(
        key="exchangerate_host", name="exchangerate.host API", kind="api",
        scope="per access key",
        metrics=("requests_per_month"),
        headers=("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"),
        on_limit="quota errors on limit",
        reset_rule="monthly quota window",
        snapshot=(("free", "monthly request quotas by plan (~)"),),
        notes="FX data: monthly quota is the meter."),
}

PROVIDERS.update(MORE_PROVIDERS_100)

# ------------------------------------------------------------- algorithms

class Clock:
    """Injectable clock so every algorithm is deterministic in tests."""
    def __init__(self, start: float = 1_000_000.0):
        self.t = start
    def now(self) -> float:
        return self.t
    def advance(self, seconds: float) -> None:
        self.t += seconds

@dataclass
class Decision:
    allowed: bool
    reason: str
    retry_after: float = 0.0
    remaining: float = 0.0

class FixedWindow:
    """Count per wall-clock window. Simple; allows a 2x burst at the edge."""
    def __init__(self, limit: int, window: float, clock: Clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.win_start, self.count = None, 0
    def allow(self, cost: int = 1) -> Decision:
        now = self.clock.now()
        start = math.floor(now / self.window) * self.window
        if start != self.win_start:
            self.win_start, self.count = start, 0
        if self.count + cost <= self.limit:
            self.count += cost
            return Decision(True, "fixed-window ok", 0.0, self.limit - self.count)
        return Decision(False, "fixed-window full", self.win_start + self.window - now,
                        self.limit - self.count)

class SlidingWindowLog:
    """Timestamp per request; precise, memory grows with rate."""
    def __init__(self, limit: int, window: float, clock: Clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.log: list[float] = []
    def allow(self, cost: int = 1) -> Decision:
        now = self.clock.now()
        self.log = [t for t in self.log if t > now - self.window]
        if len(self.log) + cost <= self.limit:
            self.log.extend([now] * cost)
            return Decision(True, "sliding-log ok", 0.0, self.limit - len(self.log))
        return Decision(False, "sliding-log full",
                        (self.log[0] + self.window - now) if self.log else self.window,
                        self.limit - len(self.log))

class SlidingWindowCounter:
    """Weighted estimate over current + previous fixed windows. Cheap."""
    def __init__(self, limit: int, window: float, clock: Clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.cur_start, self.cur, self.prev = None, 0, 0
    def _roll(self):
        now = self.clock.now()
        start = math.floor(now / self.window) * self.window
        if self.cur_start is None:
            self.cur_start = start
        elif start == self.cur_start + self.window:
            self.prev, self.cur, self.cur_start = self.cur, 0, start
        elif start > self.cur_start:
            self.prev, self.cur, self.cur_start = 0, 0, start
        return now
    def estimate(self) -> float:
        now = self._roll()
        frac = (now - self.cur_start) / self.window
        return self.prev * (1 - frac) + self.cur
    def allow(self, cost: int = 1) -> Decision:
        est = self.estimate()
        if est + cost <= self.limit:
            self.cur += cost
            return Decision(True, "sliding-counter ok", 0.0, self.limit - self.estimate())
        return Decision(False, "sliding-counter full", self.window * 0.5,
                        max(0.0, self.limit - est))

class TokenBucket:
    """Capacity + steady refill. Allows bursts, enforces the long-run rate.
    This is the shape Anthropic documents and Stripe-style APIs use."""
    def __init__(self, capacity: float, refill_per_sec: float, clock: Clock):
        self.capacity, self.rate, self.clock = capacity, refill_per_sec, clock
        self.tokens, self.last = capacity, clock.now()
    def _refill(self):
        now = self.clock.now()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
    def allow(self, cost: float = 1.0) -> Decision:
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return Decision(True, "token-bucket ok", 0.0, self.tokens)
        wait = (cost - self.tokens) / self.rate if self.rate > 0 else float("inf")
        return Decision(False, "token-bucket empty", wait, self.tokens)

class LeakyBucketQueue:
    """Requests queue and drain at a fixed rate: smooths, adds latency."""
    def __init__(self, capacity: int, drain_per_sec: float, clock: Clock):
        self.capacity, self.rate, self.clock = capacity, drain_per_sec, clock
        self.queue: list[float] = []   # scheduled drain times
    def allow(self, cost: int = 1) -> Decision:
        now = self.clock.now()
        self.queue = [t for t in self.queue if t > now]
        if len(self.queue) + cost <= self.capacity:
            base = self.queue[-1] if self.queue else now
            for i in range(cost):
                self.queue.append(base + (i + 1) / self.rate)
            return Decision(True, "leaky-bucket queued", 0.0, self.capacity - len(self.queue))
        return Decision(False, "leaky-bucket overflow", 1.0 / self.rate,
                        self.capacity - len(self.queue))

# ---------------------------------------------------------- header parsing

@dataclass
class RateState:
    provider: str = "unknown"
    limit: float | None = None
    remaining: float | None = None
    used: float | None = None
    reset_epoch: float | None = None
    retry_after: float | None = None
    limit_requests: float | None = None
    remaining_requests: float | None = None
    limit_tokens: float | None = None
    remaining_tokens: float | None = None
    resource: str | None = None

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _norm(headers: dict) -> dict:
    return {str(k).lower().strip(): str(v).strip() for k, v in headers.items()}

def parse_retry_after(headers: dict, now: float | None = None) -> float | None:
    h = _norm(headers)
    v = h.get("retry-after")
    if v is None:
        return None
    if v.replace(".", "", 1).isdigit():
        return float(v)
    try:  # HTTP-date form
        dt = parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - (now if now is not None else time.time()))
    except Exception:
        return None

def parse_headers(headers: dict, now: float | None = None) -> RateState:
    """S01: unified translator — every dialect below becomes one RateState."""
    h = _norm(headers)
    st = RateState(retry_after=parse_retry_after(headers, now))
    if "x-ratelimit-resource" in h or ("x-ratelimit-limit" in h and "x-ratelimit-reset" in h):
        st.provider = "github_rest"
        st.limit, st.remaining, st.used = _f(h.get("x-ratelimit-limit")), _f(h.get("x-ratelimit-remaining")), _f(h.get("x-ratelimit-used"))
        st.reset_epoch, st.resource = _f(h.get("x-ratelimit-reset")), h.get("x-ratelimit-resource")
        return st
    if any(k.startswith("anthropic-ratelimit-") for k in h):
        st.provider = "claude_api"
        st.limit_requests = _f(h.get("anthropic-ratelimit-requests-limit"))
        st.remaining_requests = _f(h.get("anthropic-ratelimit-requests-remaining"))
        st.limit_tokens = _f(h.get("anthropic-ratelimit-input-tokens-limit"))
        st.remaining_tokens = _f(h.get("anthropic-ratelimit-input-tokens-remaining"))
        reset = h.get("anthropic-ratelimit-requests-reset")
        if reset:
            try:
                st.reset_epoch = parsedate_to_datetime(reset).timestamp()
            except Exception:
                st.reset_epoch = _f(reset)
        return st
    if "x-ratelimit-limit-requests" in h or "x-ratelimit-limit-tokens" in h:
        # OpenAI and Meta share this family; Muse adds per-model context elsewhere.
        st.provider = "openai_or_muse"
        st.limit_requests, st.remaining_requests = _f(h.get("x-ratelimit-limit-requests")), _f(h.get("x-ratelimit-remaining-requests"))
        st.limit_tokens, st.remaining_tokens = _f(h.get("x-ratelimit-limit-tokens")), _f(h.get("x-ratelimit-remaining-tokens"))
        return st
    if "ratelimit" in h or "ratelimit-policy" in h:  # IETF draft fields
        st.provider = "ietf_draft"
        parts = {}
        for chunk in h.get("ratelimit", "").split(","):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                parts[k.strip()] = v.strip()
        st.limit, st.remaining = _f(parts.get("limit")), _f(parts.get("remaining"))
        ra = _f(parts.get("reset"))
        if ra is not None:
            st.retry_after = ra
        return st
    if "x-ratelimit-limit" in h:  # bare GitHub-style without reset
        st.provider = "github_rest"
        st.limit, st.remaining = _f(h.get("x-ratelimit-limit")), _f(h.get("x-ratelimit-remaining"))
        return st
    return st

def fingerprint_provider(headers: dict, body: str = "") -> str:
    """S02: guess which limit system answered, from headers + body shape."""
    h = _norm(headers)
    if any(k.startswith("anthropic-ratelimit-") for k in h):
        return "claude_api"
    if "x-ratelimit-resource" in h:
        return "github_rest"
    if "RESOURCE_EXHAUSTED" in body:
        return "gemini_api"
    if "rate_limit_exceeded" in body:
        return "openai_api"
    if "secondary rate limit" in body.lower():
        return "github_rest"
    if "x-ratelimit-limit-requests" in h:
        return "openai_or_muse"
    return "unknown"

def classify_429(provider: str, headers: dict, body: str = "") -> str:
    """S03: which kind of limit actually fired."""
    b = body.lower()
    if provider == "github_rest":
        if "secondary" in b:
            return "secondary_burst_limit"
        if parse_headers(headers).remaining == 0:
            return "primary_hourly_limit"
        return "github_403_needs_header_check"  # 403 is also plain forbidden
    if provider == "gemini_api":
        return "quota_first_hit_rpm_tpm_or_rpd"
    if provider == "google_flow":
        return "credit_bucket_empty"
    if provider == "claude_code":
        return "session_or_weekly_window"
    return "rate_limit"

def clock_skew_guard(reset_epoch: float, now: float, max_future: float = 7200.0) -> float:
    """S04: a reset far in the future or in the past is a clock problem, not a wait."""
    if reset_epoch is None:
        return 0.0
    wait = reset_epoch - now
    if wait < 0:
        return 0.0
    return min(wait, max_future)

# ------------------------------------------------------------- runtime mask

HOLD_ACTIONS = {
    "rotate_keys_to_evade": "Rotating keys/accounts to spend another identity's budget is evasion.",
    "spoof_ip_or_fingerprint": "Spoofing source identity to escape a per-IP limit is evasion.",
    "farm_accounts": "Creating accounts to multiply a free tier is evasion.",
    "ignore_retry_after": "Retrying inside Retry-After converts a limit into abuse.",
    "hammer_after_429": "Continuing to send after a 429 is the behaviour secondary limits exist to stop.",
    "share_claude_code_session": "Sharing one subscription session across people breaks the shared-window regime.",
    "automate_flow_ui": "Scripting the Flow consumer UI to drain credits is not an API use.",
    "split_identity_openai": "Splitting one workload across orgs to multiply per-org limits is evasion.",
    "live_scout_fetch": "Scouts read synthetic wire fixtures in this lab; scraping provider pages live is owner-gated and off by default.",
}

def gate(action: str) -> tuple[str, str]:
    if action in HOLD_ACTIONS:
        return ("HOLD", HOLD_ACTIONS[action])
    return ("PROCEED", "action is inside the published limits")

@dataclass
class MaskResult:
    outcome: str            # SENT | WAIT | CIRCUIT_OPEN | HOLD
    wait_seconds: float = 0.0
    detail: str = ""

class RuntimeMask:
    """The runtime mask: a client-side mirror bucket + 429 discipline.

    It never sends around a limit. Local bucket empty -> WAIT.
    A 429 -> honour retry-after/reset first, else exponential backoff
    with deterministic jitter. Repeated 429s -> circuit opens.
    """
    def __init__(self, bucket, clock: Clock, max_consecutive_429: int = 3,
                 base_backoff: float = 1.0, cap_backoff: float = 300.0):
        self.bucket, self.clock = bucket, clock
        self.max_429, self.base, self.cap = max_consecutive_429, base_backoff, cap_backoff
        self.consecutive_429 = 0
        self.circuit_open_until = 0.0
        self.log: list[dict] = []

    def backoff(self, attempt: int) -> float:
        # exponential with deterministic jitter (seeded by attempt) for tests
        raw = min(self.cap, self.base * (2 ** attempt))
        jitter = ((attempt * 37) % 10) / 10.0  # 0.0..0.9, deterministic
        return raw + jitter

    def before_request(self, cost: float = 1.0) -> MaskResult:
        now = self.clock.now()
        if now < self.circuit_open_until:
            return MaskResult("CIRCUIT_OPEN", self.circuit_open_until - now,
                              "circuit open after repeated 429s")
        d = self.bucket.allow(cost)
        if d.allowed:
            return MaskResult("SENT", 0.0, d.reason)
        return MaskResult("WAIT", d.retry_after, d.reason)

    def on_response(self, status: int, headers: dict | None = None,
                    provider: str = "unknown", body: str = "") -> MaskResult:
        headers = headers or {}
        if status in (200, 201, 204, 304):
            self.consecutive_429 = 0
            return MaskResult("SENT", 0.0, "ok")
        if status in (403, 429):
            kind = classify_429(provider, headers, body)
            self.consecutive_429 += 1
            self.log.append({"status": status, "kind": kind,
                             "at": self.clock.now(), "provider": provider})
            if self.consecutive_429 >= self.max_429:
                wait = self.backoff(self.consecutive_429)
                self.circuit_open_until = self.clock.now() + wait
                return MaskResult("CIRCUIT_OPEN", wait, kind)
            ra = parse_retry_after(headers, self.clock.now())
            if ra is not None:
                return MaskResult("WAIT", ra, kind + " (retry-after honoured)")
            st = parse_headers(headers, self.clock.now())
            if st.reset_epoch:
                return MaskResult("WAIT", clock_skew_guard(st.reset_epoch, self.clock.now()),
                                  kind + " (wait to reset)")
            return MaskResult("WAIT", self.backoff(self.consecutive_429), kind + " (backoff)")
        return MaskResult("SENT", 0.0, f"status {status} not a limit signal")

# ---------------------------------------------------------------- simulator

@dataclass
class SimResult:
    allowed: bool
    provider: str
    metric_hit: str | None
    status: int
    detail: str

class SyntheticProvider:
    """Offline reverse-engineering fixture: replays each provider's
    counting rules with synthetic budgets scaled down for study."""
    def __init__(self, key: str, clock: Clock):
        self.key, self.clock = key, clock
        if key == "github_rest":
            self.primary = FixedWindow(10, 3600, clock)       # scaled 5000 -> 10
            self.points = SlidingWindowLog(18, 60, clock)     # 900/min -> 18
        elif key == "gemini_api":
            self.rpm = SlidingWindowLog(5, 60, clock)
            self.tpm = TokenBucket(1000, 1000 / 60.0, clock)
            self.rpd = FixedWindow(20, 86400, clock)
        elif key in ("muse_model_api", "openai_api"):
            self.rpm = SlidingWindowLog(10, 60, clock)
            self.tpm = TokenBucket(4000, 4000 / 60.0, clock)
        elif key == "claude_api":
            self.rpm = SlidingWindowLog(10, 60, clock)
            self.itpm = TokenBucket(2000, 2000 / 60.0, clock)
            self.otpm = TokenBucket(400, 400 / 60.0, clock)
        elif key == "google_flow":
            self.daily = 50
            self.monthly = 1000
            self.costs = {"lite": 10, "fast": 20, "quality": 100}
        elif key == "claude_code":
            self.session = SlidingWindowLog(10, 5 * 3600, clock)  # prompts, scaled
            self.weekly = FixedWindow(40, 7 * 86400, clock)
        else:
            raise ValueError(f"unknown provider {key}")

    def call(self, tokens_in: int = 100, tokens_out: int = 50,
             mutation: bool = False, kind: str = "lite") -> SimResult:
        k = self.key
        if k == "github_rest":
            pts = 5 if mutation else 1
            d = self.points.allow(pts)
            if not d.allowed:
                return SimResult(False, k, "secondary_points_per_minute", 403,
                                 "secondary rate limit (points/min)")
            d = self.primary.allow(1)
            if not d.allowed:
                return SimResult(False, k, "requests_per_hour", 403, "primary rate limit")
            return SimResult(True, k, None, 200, "ok")
        if k == "gemini_api":
            for name, meter, cost in (("requests_per_day", self.rpd, 1),
                                      ("requests_per_minute", self.rpm, 1),
                                      ("tokens_per_minute", self.tpm, tokens_in + tokens_out)):
                d = meter.allow(cost)
                if not d.allowed:
                    return SimResult(False, k, name, 429, "RESOURCE_EXHAUSTED: " + name)
            return SimResult(True, k, None, 200, "ok")
        if k in ("muse_model_api", "openai_api"):
            d = self.rpm.allow(1)
            if not d.allowed:
                return SimResult(False, k, "requests_per_minute", 429, "rpm")
            d = self.tpm.allow(tokens_in + tokens_out)
            if not d.allowed:
                return SimResult(False, k, "tokens_per_minute", 429, "tpm")
            return SimResult(True, k, None, 200, "ok")
        if k == "claude_api":
            d = self.rpm.allow(1)
            if not d.allowed:
                return SimResult(False, k, "requests_per_minute", 429, "rpm")
            d = self.itpm.allow(tokens_in)   # cache reads excluded by caller passing 0
            if not d.allowed:
                return SimResult(False, k, "input_tokens_per_minute", 429, "itpm")
            d = self.otpm.allow(tokens_out)
            if not d.allowed:
                return SimResult(False, k, "output_tokens_per_minute", 429, "otpm")
            return SimResult(True, k, None, 200, "ok")
        if k == "google_flow":
            cost = self.costs[kind]
            if self.daily >= cost:
                self.daily -= cost
                return SimResult(True, k, None, 200, f"daily credits, {self.daily} left")
            if self.monthly >= cost:
                self.monthly -= cost
                return SimResult(True, k, None, 200, f"monthly credits, {self.monthly} left")
            return SimResult(False, k, "credits", 429, "You've reached your usage limit")
        if k == "claude_code":
            d = self.session.allow(1)
            if not d.allowed:
                return SimResult(False, k, "five_hour_session_window", 429, "session limit")
            d = self.weekly.allow(1)
            if not d.allowed:
                return SimResult(False, k, "weekly_window", 429, "weekly limit")
            return SimResult(True, k, None, 200, "ok")
        raise AssertionError(k)

# ------------------------------------------- 20 more suggestions (S01-S20)
# S01 unified header translator      -> parse_headers (above)
# S02 provider fingerprinting        -> fingerprint_provider (above)
# S03 429 body classifier            -> classify_429 (above)
# S04 clock-skew guard               -> clock_skew_guard (above)
# S05-S20 implemented below.

def forecast_exhaustion(remaining: float, rate_per_min: float) -> float | None:
    """S05: minutes until the budget is gone at the current pace."""
    if rate_per_min <= 0:
        return None
    return remaining / rate_per_min

def pace_rank(jobs: list[dict]) -> list[dict]:
    """S06: cost-aware pacing — cheapest (fewest tokens) first."""
    return sorted(jobs, key=lambda j: j.get("tokens_in", 0) + j.get("tokens_out", 0))

class DedupeCache:
    """S07: identical GETs collapse to one call; GitHub ETag/304 pattern."""
    def __init__(self):
        self.store: dict[str, str] = {}
    def get(self, key: str):
        return self.store.get(key)
    def put(self, key: str, etag: str) -> None:
        self.store[key] = etag
    def conditional_headers(self, key: str) -> dict:
        etag = self.store.get(key)
        return {"If-None-Match": etag} if etag else {}

class PriorityQueueMask:
    """S08: interactive work jumps batch work when budget is scarce."""
    ORDER = {"interactive": 0, "normal": 1, "batch": 2}
    def __init__(self):
        self.jobs: list[dict] = []
    def push(self, job: dict) -> None:
        self.jobs.append(job)
        self.jobs.sort(key=lambda j: self.ORDER.get(j.get("priority", "normal"), 1))
    def pop(self):
        return self.jobs.pop(0) if self.jobs else None

def deadline_filter(jobs: list[dict], now: float, reset_epoch: float) -> tuple[list, list]:
    """S09: keep jobs that can finish before reset; defer the rest."""
    keep, defer = [], []
    for j in jobs:
        (keep if now + j.get("est_seconds", 0) <= reset_epoch else defer).append(j)
    return keep, defer

def polling_advisor(provider: str, poll_per_hour: int) -> str:
    """S10: webhooks/events beat polling where the platform offers them."""
    if provider == "github_rest" and poll_per_hour > 60:
        return "switch to webhooks or the Events API; polling at this rate burns the primary budget"
    return "polling is within budget"

def batch_advisor(provider: str, n_requests: int) -> str:
    """S11: batch endpoints trade latency for a separate, larger window."""
    if provider in ("openai_api", "claude_api") and n_requests >= 100:
        return "use the provider Batch API: separate queue, not the synchronous RPM/TPM window"
    return "synchronous path is fine at this volume"

def effective_input_tokens(tokens_in: int, cached_tokens: int, provider: str) -> int:
    """S12: cache accounting differs — Anthropic/Muse-style cache reads
    do not count toward input-token meters; OpenAI's TPM counts them."""
    if provider in ("claude_api", "muse_model_api"):
        return max(0, tokens_in - cached_tokens)
    return tokens_in

def bucket_key(provider: str, model: str, project: str = "", team: str = "") -> str:
    """S13+S14: isolation key matching each provider's real scope."""
    scope = {"gemini_api": project or "default-project",
             "muse_model_api": team or "default-team",
             "openai_api": project or "default-project",
             "claude_api": "org", "github_rest": "user",
             "google_flow": "account", "claude_code": "account"}[provider]
    return f"{provider}:{scope}:{model}"

def reset_calendar(provider: str) -> dict:
    """S15: when each meter actually resets, in one place."""
    return {
        "github_rest": {"primary": "rolling hour to x-ratelimit-reset", "secondary": "per-minute"},
        "gemini_api": {"rpm_tpm": "rolling 60s", "rpd": "midnight Pacific"},
        "muse_model_api": {"rpm_tpm": "per-minute window"},
        "google_flow": {"daily": "first generation of the day + 24h, no rollover", "monthly": "billing cycle"},
        "openai_api": {"rpm_tpm": "per-minute, continuous recovery", "daily": "per day"},
        "claude_api": {"all": "continuous token-bucket refill"},
        "claude_code": {"session": "rolling 5h from first message", "weekly": "rolling 7 days"},
    }[provider]

def alert_level(remaining: float, limit: float) -> str:
    """S16: 50 / 80 / 95 percent thresholds."""
    if limit <= 0:
        return "unknown"
    used = 1 - (remaining / limit)
    if used >= 0.95:
        return "critical"
    if used >= 0.80:
        return "warning"
    if used >= 0.50:
        return "notice"
    return "ok"

class IncidentLog:
    """S17: every limit event, recorded with its classified cause."""
    def __init__(self):
        self.events: list[dict] = []
    def record(self, provider: str, kind: str, at: float) -> None:
        self.events.append({"provider": provider, "kind": kind, "at": at})
    def summary(self) -> dict:
        out: dict[str, int] = {}
        for e in self.events:
            out[e["kind"]] = out.get(e["kind"], 0) + 1
        return out

def what_if_burst(provider: str, n: int, **kw) -> dict:
    """S18: dry-run a burst against the synthetic provider, offline."""
    sim = SyntheticProvider(provider, Clock())
    ok = sum(1 for _ in range(n) if sim.call(**kw).allowed)
    return {"sent": n, "allowed": ok, "limited": n - ok}

def export_policy(provider: str) -> dict:
    """S19: the mask policy for one provider, as portable JSON."""
    p = PROVIDERS[provider]
    return {"provider": p.key, "scope": p.scope, "metrics": list(p.metrics),
            "reset_rule": p.reset_rule, "snapshot_date": SNAPSHOT_DATE,
            "policy": "pace inside limits; honour retry-after; never evade"}

TIER_LADDERS = {
    "openai_api": (("Free", 0), ("Build", 5), ("Launch", 100), ("Grow", 500)),
    "gemini_api": (("Free", 0), ("Tier 1", 1), ("Tier 2", 2), ("Tier 3", 3)),
}

def tier_upgrade_math(provider: str, total_spent: float = 0.0) -> dict:
    """S20: what the next tier costs, from the published ladder."""
    ladder = TIER_LADDERS.get(provider)
    if not ladder:
        return {"provider": provider, "note": "no self-serve spend ladder published"}
    for i, (name, need) in enumerate(ladder):
        if total_spent < need:
            return {"provider": provider, "next_tier": name,
                    "needs_total": need, "still_to_spend": need - total_spent}
    return {"provider": provider, "note": "top self-serve tier reached"}

SUGGESTIONS_20 = [
    ("S01", "Unified header translator (all dialects -> one RateState)", "IMPLEMENTED"),
    ("S02", "Provider fingerprinting from headers + body", "IMPLEMENTED"),
    ("S03", "429 classifier: primary vs secondary vs quota vs credit vs session", "IMPLEMENTED"),
    ("S04", "Clock-skew guard on reset timestamps", "IMPLEMENTED"),
    ("S05", "Budget forecast: time-to-exhaustion at current pace", "IMPLEMENTED"),
    ("S06", "Cost-aware pacing: cheapest jobs first", "IMPLEMENTED"),
    ("S07", "Request dedupe + ETag conditional cache (304 pattern)", "IMPLEMENTED"),
    ("S08", "Priority queue: interactive > normal > batch", "IMPLEMENTED"),
    ("S09", "Deadline-aware scheduler vs reset time", "IMPLEMENTED"),
    ("S10", "Webhook-over-polling advisor", "IMPLEMENTED"),
    ("S11", "Batch API advisor for bulk jobs", "IMPLEMENTED"),
    ("S12", "Cache-hit accounting per provider", "IMPLEMENTED"),
    ("S13", "Per-model bucket isolation", "IMPLEMENTED"),
    ("S14", "Shared-scope guard: per-team / per-org / per-project keys", "IMPLEMENTED"),
    ("S15", "Reset calendar for all seven systems", "IMPLEMENTED"),
    ("S16", "Alert thresholds at 50 / 80 / 95 percent", "IMPLEMENTED"),
    ("S17", "Incident log with classified causes", "IMPLEMENTED"),
    ("S18", "Dry-run what-if burst simulator", "IMPLEMENTED"),
    ("S19", "Portable JSON policy export", "IMPLEMENTED"),
    ("S20", "Tier-upgrade math from published ladders", "IMPLEMENTED"),
]

# ------------------------------------------------- unified tracker (all-in-one)

@dataclass
class CallRecord:
    provider: str
    model: str
    status: int
    latency_ms: float
    bytes_in: int = 0
    bytes_out: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    credits: float = 0.0
    at: float = 0.0
    error_kind: str | None = None

def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(p / 100.0 * len(s)) - 1))
    return s[idx]

def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"

class UnifiedTracker:
    """ALL APIs checked into ONE tracker: usage, speed, size, errors,
    limits — per provider, per model, and rolled up globally.
    Records are fed by the runtime (synthetic in this lab); no live calls.
    """
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.records: list[CallRecord] = []

    def record(self, provider: str, model: str = "default", status: int = 200,
               latency_ms: float = 0.0, bytes_in: int = 0, bytes_out: int = 0,
               tokens_in: int = 0, tokens_out: int = 0, credits: float = 0.0,
               error_kind: str | None = None) -> CallRecord:
        if provider not in PROVIDERS:
            raise ValueError(f"unregistered provider {provider}")
        r = CallRecord(provider, model, status, float(latency_ms), int(bytes_in),
                       int(bytes_out), int(tokens_in), int(tokens_out), float(credits),
                       self.clock.now(), error_kind)
        self.records.append(r)
        return r

    def _stats(self, recs: list[CallRecord]) -> dict:
        n = len(recs)
        if n == 0:
            return {"calls": 0}
        lat = [r.latency_ms for r in recs]
        dur_s = max(1.0, (max(r.at for r in recs) - min(r.at for r in recs)) or 1.0)
        total_bytes = sum(r.bytes_in + r.bytes_out for r in recs)
        total_tokens = sum(r.tokens_in + r.tokens_out for r in recs)
        ok = sum(1 for r in recs if 200 <= r.status < 300 or r.status == 304)
        limited = sum(1 for r in recs if r.status in (403, 429))
        errors = sum(1 for r in recs if r.status >= 400)
        return {
            "calls": n, "ok": ok, "errors": errors, "limited": limited,
            "success_rate": round(ok / n, 4), "error_rate": round(errors / n, 4),
            "limit_rate": round(limited / n, 4),
            "latency_ms_avg": round(sum(lat) / n, 2),
            "latency_ms_p50": _pct(lat, 50), "latency_ms_p95": _pct(lat, 95),
            "latency_ms_p99": _pct(lat, 99), "latency_ms_max": max(lat),
            "bytes_in": sum(r.bytes_in for r in recs),
            "bytes_out": sum(r.bytes_out for r in recs),
            "bytes_total": total_bytes, "data_size": human_bytes(total_bytes),
            "avg_bytes_per_call": round(total_bytes / n, 1),
            "tokens_in": sum(r.tokens_in for r in recs),
            "tokens_out": sum(r.tokens_out for r in recs),
            "tokens_total": total_tokens,
            "avg_tokens_per_call": round(total_tokens / n, 1),
            "credits": sum(r.credits for r in recs),
            "requests_per_minute_observed": round(n / (dur_s / 60.0), 2),
            "tokens_per_second_observed": round(total_tokens / dur_s, 2),
            "bytes_per_second_observed": round(total_bytes / dur_s, 2),
        }

    def per_provider(self) -> dict:
        out = {}
        for key in PROVIDERS:
            recs = [r for r in self.records if r.provider == key]
            out[key] = self._stats(recs)
        return out

    def per_model(self) -> dict:
        keys = sorted({(r.provider, r.model) for r in self.records})
        return {f"{p}:{m}": self._stats([r for r in self.records
                                          if r.provider == p and r.model == m])
                for p, m in keys}

    def analytics(self) -> dict:
        per = self.per_provider()
        active = {k: v for k, v in per.items() if v.get("calls", 0) > 0}
        rank = lambda field: sorted(active, key=lambda k: active[k].get(field, 0), reverse=True)
        return {
            "global": self._stats(self.records),
            "per_provider": per,
            "per_model": self.per_model(),
            "rankings": {
                "most_calls": rank("calls"),
                "most_data": rank("bytes_total"),
                "most_tokens": rank("tokens_total"),
                "slowest_p95": rank("latency_ms_p95"),
                "most_limited": rank("limited"),
                "highest_error_rate": rank("error_rate"),
            },
            "snapshot_date": SNAPSHOT_DATE,
        }

    def check_all(self) -> list[dict]:
        """One manageable table: every API, its limit system + live stats."""
        per = self.per_provider()
        rows = []
        for key, p in PROVIDERS.items():
            s = per[key]
            rows.append({
                "provider": key, "name": p.name, "kind": p.kind, "scope": p.scope,
                "metrics": list(p.metrics), "calls": s.get("calls", 0),
                "success_rate": s.get("success_rate"), "limited": s.get("limited", 0),
                "p95_ms": s.get("latency_ms_p95"), "data_size": s.get("data_size", "0.0 B"),
                "tokens_total": s.get("tokens_total", 0), "credits": s.get("credits", 0.0),
                "health": ("no data" if s.get("calls", 0) == 0 else
                           "limited" if s.get("limit_rate", 0) > 0 else
                           "errors" if s.get("error_rate", 0) > 0 else "ok"),
            })
        return rows

    def render_table(self) -> str:
        rows = self.check_all()
        head = f"{'provider':16} {'calls':>5} {'ok%':>6} {'429/403':>7} {'p95ms':>7} {'data':>9} {'tokens':>8}  health"
        lines = [head, "-" * len(head)]
        for r in rows:
            ok = "-" if r["success_rate"] is None else f"{r['success_rate'] * 100:.0f}"
            p95 = "-" if r["p95_ms"] is None else f"{r['p95_ms']:.0f}"
            lines.append(f"{r['provider']:16} {r['calls']:>5} {ok:>6} {r['limited']:>7} "
                         f"{p95:>7} {r['data_size']:>9} {r['tokens_total']:>8}  {r['health']}")
        return "\n".join(lines)

    def to_csv(self) -> str:
        rows = self.check_all()
        cols = ["provider", "calls", "success_rate", "limited", "p95_ms",
                "data_size", "tokens_total", "credits", "health"]
        out = [",".join(cols)]
        for r in rows:
            out.append(",".join(str(r[c]) for c in cols))
        return "\n".join(out)

# ------------------------------------------- brains: Argon 4 + Muse, together

import hashlib, os

@dataclass
class BrainDecision:
    verdict: str            # PROCEED | TIGHTEN | HOLD
    action: str
    argon_trace: dict
    muse_advice: list
    final_reason: str

class Argon4Decider:
    """Gemini Argon 4 Extended Thinking — the DECIDER seat.
    House pattern: Carbon 4 seat, engine gemini-2.5-pro, model
    google/gemini-carbon-4-extended-pro-thinking, MAP->MODEL->STRESS->DECIDE,
    thinking tiers 2048 / 8192 / 32768 by stakes. Offline structure is
    authoritative; a live key is detected by NAME presence only and a
    live call can only ever tighten a verdict, never loosen one.
    """
    NAME = "Gemini Argon 4 Extended Thinking"
    ENGINE = "gemini-2.5-pro"
    MODEL = "google/gemini-carbon-4-extended-pro-thinking"
    TIERS = (2048, 8192, 32768)

    def tier_for(self, stakes: str) -> int:
        return {"routine": 2048, "project": 8192, "deploy": 32768}.get(stakes, 8192)

    def decide(self, action: str, tracker: UnifiedTracker, stakes: str = "project") -> dict:
        key_present = "GEMINI_API_KEY" in os.environ  # name only, value never read
        stages = {}
        stages["MAP"] = {"action": action, "providers": list(PROVIDERS),
                         "live_key_present": key_present, "live_calls": 0}
        stages["MODEL"] = {"analytics": tracker.analytics()["global"],
                           "per_provider_health": [r["health"] for r in tracker.check_all()]}
        gate_verdict, gate_reason = gate(action)
        stressed = []
        g = tracker.analytics()["global"]
        if g.get("limit_rate", 0) and g["limit_rate"] > 0.2:
            stressed.append("limit_rate above 20%: pacing is too hot")
        if g.get("error_rate", 0) and g["error_rate"] > 0.3:
            stressed.append("error_rate above 30%: stop and inspect before more calls")
        stages["STRESS"] = {"gate": gate_verdict, "warnings": stressed}
        if gate_verdict == "HOLD":
            verdict, reason = "HOLD", gate_reason
        elif stressed:
            verdict, reason = "TIGHTEN", "; ".join(stressed)
        else:
            verdict, reason = "PROCEED", "inside published limits, tracker healthy"
        stages["DECIDE"] = {"verdict": verdict, "reason": reason,
                            "tier": self.tier_for(stakes)}
        trace_hash = hashlib.sha256(json.dumps(stages, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return {"seat": self.NAME, "engine": self.ENGINE, "model": self.MODEL,
                "tier": self.tier_for(stakes), "stages": stages,
                "trace_hash": trace_hash, "verdict": verdict, "reason": reason}

class MuseAdvisor:
    """Muse — the advisor/manager seat. muse-spark-1.1 / muse-wonder-1.0,
    MODEL_API_KEY by name presence only, authority NONE: exactly 4 advice
    items, can tighten, can never override an Argon HOLD.
    """
    NAME = "Muse"
    MODELS = ("muse-spark-1.1", "muse-wonder-1.0")
    AUTHORITY = "none"

    def advise(self, action: str, tracker: UnifiedTracker) -> list[str]:
        key_present = "MODEL_API_KEY" in os.environ  # name only, value never read
        g = tracker.analytics()["global"]
        return [
            f"Tracker evidence first: {g.get('calls', 0)} calls, limit_rate={g.get('limit_rate', 0)} — decide from data, not vibes.",
            "Keep the mask's discipline: honour Retry-After before any backoff math.",
            "Prefer the cheapest healthy provider/model in the rankings for batch work.",
            f"Live Muse seat {'detected by name' if key_present else 'not configured'}; advice stays advisory either way.",
        ]

class Council:
    """Argon 4 DECIDES, Muse ADVISES — managing the full project together.
    Strictest verdict wins; every decision is logged with its trace hash.
    """
    ORDER = {"PROCEED": 0, "TIGHTEN": 1, "HOLD": 2}
    def __init__(self, tracker: UnifiedTracker):
        self.tracker = tracker
        self.argon = Argon4Decider()
        self.muse = MuseAdvisor()
        self.decision_log: list[dict] = []

    def decide(self, action: str, stakes: str = "project") -> BrainDecision:
        a = self.argon.decide(action, self.tracker, stakes)
        advice = self.muse.advise(action, self.tracker)
        verdict = a["verdict"]  # Muse holds no authority to change it
        d = BrainDecision(verdict, action, a, advice,
                          a["reason"] if verdict != "PROCEED" else
                          "Argon PROCEED; Muse advice attached, none blocking")
        self.decision_log.append({"action": action, "verdict": verdict,
                                  "trace_hash": a["trace_hash"], "tier": a["tier"]})
        return d

    def manage_project(self, steps: list[str], stakes: str = "deploy") -> dict:
        """Full-project pass: every step decided, tracker state attached."""
        decisions = [asdict(self.decide(s, stakes)) for s in steps]
        worst = max((d["verdict"] for d in decisions),
                    key=lambda v: self.ORDER[v], default="PROCEED")
        return {"project_verdict": worst, "steps": len(steps),
                "decisions": decisions, "tracker_table": self.tracker.render_table(),
                "managed_by": [self.argon.NAME, self.muse.NAME]}

# ------------------------------------- local models inside: SLM, LLM, ML,
# ------------------------------------- NNL — fused by the local A.I layer
# From-scratch, pure-stdlib, educational miniatures — honestly labelled:
# the SLM is a bigram summarizer, the LLM a trigram drafter, the ML a
# softmax regression, the NNL a tiny backprop MLP. None is a production
# model; all are deterministic, offline, and advisory only (authority
# none — Argon 4 decides, HOLD gates stand, exactly like the Muse seat).

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())

def provider_text(key: str) -> str:
    p = PROVIDERS[key]
    snap = ". ".join(f"{label} is {value}" for label, value in p.snapshot)
    return (f"{p.name}. Scope: {p.scope}. Counted in {', '.join(p.metrics)}. "
            f"Snapshot: {snap}. Reset rule: {p.reset_rule}. {p.notes}")

class TinySLM:
    """SLM — small language model: bigram-ranked extractive summarizer
    over the registry corpus. Fast, tiny, runs anywhere."""
    NAME = "TinySLM (from-scratch bigram summarizer)"
    def __init__(self):
        self.bigrams: dict = {}
        toks = _tokens(" ".join(provider_text(k) for k in PROVIDERS))
        for a, b in zip(toks, toks[1:]):
            self.bigrams[(a, b)] = self.bigrams.get((a, b), 0) + 1

    def summarize(self, provider: str, max_sentences: int = 2) -> str:
        sentences = [s.strip() for s in re.split(r"[.!?]\s+", provider_text(provider))
                     if len(s.strip()) > 25]
        def score(s: str) -> float:
            t = _tokens(s)
            return sum(self.bigrams.get((a, b), 0) for a, b in zip(t, t[1:])) / max(1, len(t))
        ranked = sorted(sentences, key=score, reverse=True)
        return ". ".join(ranked[:max_sentences]) + ("." if ranked else "")

class TinyLLM:
    """LLM — large(er) language model seat: a trigram drafter over the
    whole registry corpus, seeded sampling, deterministic per seed text.
    Educational miniature — not a production LLM."""
    NAME = "TinyLLM (from-scratch trigram drafter, educational miniature)"
    def __init__(self):
        self.tri: dict = {}
        toks = _tokens(" ".join(provider_text(k) for k in PROVIDERS))
        for a, b, c in zip(toks, toks[1:], toks[2:]):
            self.tri.setdefault((a, b), []).append(c)

    def generate(self, seed_text: str, words: int = 28) -> str:
        seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        toks = _tokens(seed_text)
        out = toks[-2:] if len(toks) >= 2 else ["rate", "limits"]
        while len(out) < words:
            nxt = self.tri.get((out[-2], out[-1]))
            if not nxt:
                break
            out.append(rng.choice(nxt))
        return " ".join(out)

def _features(stats: dict) -> list[float]:
    sr = stats.get("success_rate")
    sr = 1.0 if sr is None else float(sr)
    return [float(stats.get("error_rate", 0) or 0),
            float(stats.get("limit_rate", 0) or 0),
            min(1.0, float(stats.get("latency_ms_p95", 0) or 0) / 5000.0),
            1.0 - sr]

class MLHealthClassifier:
    """ML — softmax regression trained by gradient descent on a
    rule-generated synthetic set: tracker stats -> ok | warn | critical."""
    NAME = "ML (from-scratch softmax regression)"
    CLASSES = ("ok", "warn", "critical")
    def __init__(self, samples_n: int = 210, epochs: int = 150):
        rng = random.Random(42)
        samples, counts = [], [0, 0, 0]
        quota = samples_n // 3
        draws = 0
        while min(counts) < quota and draws < 20000:
            draws += 1
            er, lr, p95 = rng.random() * 0.8, rng.random() * 0.7, rng.random()
            x = [er, lr, p95, min(1.0, er + lr * 0.8)]
            y = 2 if (lr > 0.25 or er > 0.45) else (1 if (lr > 0.05 or er > 0.15 or p95 > 0.45) else 0)
            if counts[y] >= quota:
                continue
            counts[y] += 1
            samples.append((x, y))
        self.w = [[0.0] * 3 for _ in range(4)]
        self.b = [0.0] * 3
        for _ in range(epochs):
            for x, y in samples:
                logits = [sum(x[i] * self.w[i][j] for i in range(4)) + self.b[j] for j in range(3)]
                mx = max(logits)
                ex = [math.exp(v - mx) for v in logits]
                tot = sum(ex) or 1.0
                for j in range(3):
                    g = ex[j] / tot - (1.0 if j == y else 0.0)
                    for i in range(4):
                        self.w[i][j] -= 0.5 * g * x[i]
                    self.b[j] -= 0.5 * g

    def classify(self, stats: dict) -> tuple[str, dict]:
        x = _features(stats)
        logits = [sum(x[i] * self.w[i][j] for i in range(4)) + self.b[j] for j in range(3)]
        mx = max(logits)
        ex = [math.exp(v - mx) for v in logits]
        tot = sum(ex) or 1.0
        probs = {self.CLASSES[j]: round(ex[j] / tot, 4) for j in range(3)}
        return self.CLASSES[max(range(3), key=lambda j: ex[j])], probs

class TinyMLP:
    """NNL — neural network layer: a 4-8-1 MLP trained with backprop on
    synthetic stat windows; outputs a 0..1 limit-risk score."""
    NAME = "NNL (from-scratch 4-8-1 MLP, backprop)"
    def __init__(self, samples_n: int = 120, epochs: int = 80):
        rng = random.Random(7)
        self.w1 = [[rng.uniform(-0.6, 0.6) for _ in range(8)] for _ in range(4)]
        self.b1 = [0.0] * 8
        self.w2 = [rng.uniform(-0.6, 0.6) for _ in range(8)]
        self.b2 = 0.0
        r2 = random.Random(11)
        data = []
        for _ in range(samples_n):
            er, lr, p95 = r2.random() * 0.8, r2.random() * 0.7, r2.random()
            x = [er, lr, p95, min(1.0, er + lr * 0.8)]
            y = 1.0 if (lr > 0.22 or er > 0.4) else (0.55 if (lr > 0.05 or er > 0.15) else 0.05)
            data.append((x, y))
        for _ in range(epochs):
            for x, y in data:
                h = [math.tanh(sum(x[i] * self.w1[i][j] for i in range(4)) + self.b1[j])
                     for j in range(8)]
                o = 1.0 / (1.0 + math.exp(-(sum(h[j] * self.w2[j] for j in range(8)) + self.b2)))
                d = (o - y) * o * (1.0 - o)
                for j in range(8):
                    dh = d * self.w2[j] * (1.0 - h[j] ** 2)
                    for i in range(4):
                        self.w1[i][j] -= 0.3 * dh * x[i]
                    self.b1[j] -= 0.3 * dh
                    self.w2[j] -= 0.3 * d * h[j]
                self.b2 -= 0.3 * d

    def risk(self, stats: dict) -> float:
        x = _features(stats)
        h = [math.tanh(sum(x[i] * self.w1[i][j] for i in range(4)) + self.b1[j])
             for j in range(8)]
        return 1.0 / (1.0 + math.exp(-(sum(h[j] * self.w2[j] for j in range(8)) + self.b2)))

class LocalAI:
    """A.I — the local fusion layer: SLM summarizes, LLM drafts, ML
    classifies health, NNL scores risk. One brief per provider.
    Authority: none — advisory only, never overrides a HOLD."""
    NAME = "A.I (local fusion: SLM + LLM + ML + NNL, advisory only)"
    def __init__(self, tracker: UnifiedTracker | None = None):
        self.slm, self.llm = TinySLM(), TinyLLM()
        self.ml, self.nnl = MLHealthClassifier(), TinyMLP()
        self.tracker = tracker

    def brief(self, provider: str, stats: dict | None = None) -> dict:
        if stats is None:
            stats = (self.tracker.analytics()["per_provider"][provider]
                     if self.tracker is not None else {})
        label, probs = self.ml.classify(stats)
        return {"provider": provider, "name": PROVIDERS[provider].name,
                "slm_summary": self.slm.summarize(provider),
                "llm_briefing": self.llm.generate(f"{PROVIDERS[provider].name} rate limits", 26),
                "ml_health": label, "ml_confidence": probs,
                "nnl_risk": round(self.nnl.risk(stats), 4),
                "models": [TinySLM.NAME, TinyLLM.NAME, MLHealthClassifier.NAME, TinyMLP.NAME],
                "authority": "none — advisory only; Argon 4 decides, HOLD gates stand"}

# ------------------------------------------------ scout swarm: rules,
# ------------------------------------------------ news and updates watch
# 250,000 VIRTUAL scout bots sharded across every watch: for each of the
# 57 providers, three watches — RULES (rate-limit docs), NEWS (changelog
# & announcements), UPDATES (status & update feeds). A cycle reads a
# synthetic wire (fixtures, labelled), diffs it against the dated
# registry snapshot, grades impact, and drafts findings with the local
# models. Live fetching is HOLD-gated: zero live calls, like everything
# else in this lab. A detected change is PROPOSED, never applied — the
# registry stays the dated snapshot until the owner re-checks live docs.

WATCH_KINDS = ("rules", "news", "updates")
_WATCH_LABEL = {"rules": "official rate-limit documentation",
                "news": "official changelog & news",
                "updates": "official status & update feed"}
WATCH_SOURCES = [{"provider": k, "kind": kind, "watch": _WATCH_LABEL[kind],
                  "name": PROVIDERS[k].name}
                 for k in PROVIDERS for kind in WATCH_KINDS]

# Synthetic wire fixtures — NOT real news; labelled so no finding can be
# mistaken for a live event.
SYNTHETIC_WIRE = [
    {"provider": "gemini_api", "kind": "rules",
     "headline": "SYNTHETIC FIXTURE: Gemini free Flash daily cap change on the practice wire",
     "snapshot_label": "free_2.5_flash",
     "new": "10 RPM / 250,000 TPM / 300 RPD"},
    {"provider": "github_rest", "kind": "updates",
     "headline": "SYNTHETIC FIXTURE: GitHub publishes a secondary rate limits explainer update"},
    {"provider": "openai_api", "kind": "news",
     "headline": "SYNTHETIC FIXTURE: OpenAI announces a new usage tier on the practice wire"},
]

def _impact(item: dict) -> str:
    text = (item.get("headline", "") + " " + item.get("kind", "")).lower()
    lowered = any(w in text for w in ("lower", "reduc", "cut", "decreas", "drop"))
    if item["kind"] == "rules" and lowered:
        return "high"
    if item["kind"] in ("rules", "updates"):
        return "medium"
    return "low"

class ScoutSwarm:
    """The bee-keeping layer: virtual bots, real bookkeeping. Each cycle
    fans the wire out across the shards, diffs, grades and drafts."""
    VIRTUAL_BOTS = 250_000
    def __init__(self, ai: LocalAI | None = None):
        self.ai = ai or LocalAI()
        self.cycles = 0
        self.findings: list[dict] = []

    def bots_per_watch(self) -> int:
        return self.VIRTUAL_BOTS // len(WATCH_SOURCES)

    def fetch_live(self, provider: str) -> tuple[str, str]:
        return gate("live_scout_fetch")

    def cycle(self, wire: list[dict] | None = None) -> list[dict]:
        wire = SYNTHETIC_WIRE if wire is None else wire
        self.cycles += 1
        out = []
        for i, item in enumerate(wire):
            prov = item["provider"]
            current = dict(PROVIDERS[prov].snapshot)
            change = None
            label = item.get("snapshot_label")
            if label and item.get("new") and current.get(label) != item["new"]:
                change = f"{label}: {current.get(label)} -> {item['new']}"
            out.append({
                "provider": prov, "kind": item["kind"], "headline": item["headline"],
                "change": change, "impact": _impact(item),
                "summary": self.ai.slm.summarize(prov, 1),
                "briefing": self.ai.llm.generate(item["headline"], 22),
                "registry_action": ("proposed — registry stays the dated snapshot until "
                                    "the owner re-checks live docs" if change else "none"),
                "shard": (f"virtual bots {i * self.bots_per_watch()}-"
                          f"{(i + 1) * self.bots_per_watch() - 1}"),
            })
        self.findings.extend(out)
        return out

    def status(self) -> dict:
        return {"bots_virtual": self.VIRTUAL_BOTS, "watches": len(WATCH_SOURCES),
                "providers_watched": len(PROVIDERS), "cycles": self.cycles,
                "findings": len(self.findings), "live_calls": 0}

# --------------------------------- one-size-fits-all mask, HoneypotX,
# --------------------------------- sandbox-api chamber, auto-save + reboot

DYNAMIC_PROVIDERS: dict[str, dict] = {}   # learned at runtime, unregistered APIs

class UniversalMask:
    """ONE mask that fits ALL APIs — registered or not.

    Registered provider -> registry mode (its published scope/metrics).
    Unknown API -> generic mode: the mask learns from the response
    headers themselves (x-ratelimit family / IETF fields / Retry-After),
    registers a dynamic entry, and paces from what it learned. If an API
    publishes nothing at all, the floor is still safe: honour 429 +
    Retry-After, back off, circuit-break. One interface, every product,
    national or international — the fit comes from the headers, not
    from a per-vendor code branch.
    """
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.masks: dict[str, RuntimeMask] = {}
        self.learned: dict[str, dict] = {}

    def fit(self, provider: str) -> str:
        return "registry" if provider in PROVIDERS else "generic_learned"

    def mask_for(self, provider: str) -> RuntimeMask:
        if provider not in self.masks:
            capacity = 60.0  # neutral starter bucket; recalibrated by headers
            self.masks[provider] = RuntimeMask(
                TokenBucket(capacity, capacity / 60.0, self.clock), self.clock)
        return self.masks[provider]

    def before(self, provider: str, cost: float = 1.0) -> MaskResult:
        return self.mask_for(provider).before_request(cost)

    def after(self, provider: str, status: int, headers: dict | None = None,
              body: str = "") -> MaskResult:
        headers = headers or {}
        st = parse_headers(headers, self.clock.now())
        if provider not in PROVIDERS:
            learned = {"limit_requests": st.limit_requests or st.limit,
                       "remaining_requests": st.remaining_requests or st.remaining,
                       "limit_tokens": st.limit_tokens,
                       "retry_after_seen": st.retry_after,
                       "first_seen_snapshot": SNAPSHOT_DATE}
            DYNAMIC_PROVIDERS[provider] = learned
            self.learned[provider] = learned
        return self.mask_for(provider).on_response(status, headers, provider, body)

    def coverage(self) -> dict:
        return {"registered_providers": len(PROVIDERS),
                "core_deep_modelled": len(CORE_PROVIDERS),
                "dynamic_learned": sorted(DYNAMIC_PROVIDERS),
                "generic_fallback": "any unregistered API: header-learned pacing + 429 discipline",
                "one_interface": "fit/before/after for every provider"}

# --- HoneypotX (merged): LOG-ONLY analysis of synthetic TEST-NET logs.
# No listener is ever opened, no bait is ever reachable: listener_open
# is always False and access_granted is always False. It watches for
# the abuse shapes a rate-limit mask cares about: scanners probing for
# keys, credential harvesters, and burst hammering after a 429.
HONEYPOT_ENTRIES = [
    {"ip": "198.51.100.11", "path": "/.env", "kind": "env_harvest", "status": 404, "hits": 6},
    {"ip": "198.51.100.12", "path": "/wp-login.php", "kind": "scanner", "status": 404, "hits": 9},
    {"ip": "198.51.100.13", "path": "/api/keys", "kind": "env_harvest", "status": 403, "hits": 4},
    {"ip": "198.51.100.14", "path": "/api/v1/models", "kind": "rate_probe", "status": 429, "hits": 40},
    {"ip": "198.51.100.15", "path": "/../../etc/passwd", "kind": "lfi_probe", "status": 400, "hits": 3},
    {"ip": "198.51.100.16", "path": "/search?q=1 OR 1=1", "kind": "sqli_probe", "status": 400, "hits": 2},
    {"ip": "198.51.100.17", "path": "/upload", "kind": "upload_attempt", "status": 403, "hits": 1},
    {"ip": "198.51.100.18", "path": "/api/chat", "kind": "hammer_after_429", "status": 429, "hits": 55},
    {"ip": "198.51.100.19", "path": "/.git/config", "kind": "env_harvest", "status": 404, "hits": 5},
    {"ip": "198.51.100.20", "path": "/actuator/env", "kind": "scanner", "status": 404, "hits": 7},
    {"ip": "198.51.100.21", "path": "/api/tokens/rotate", "kind": "rate_probe", "status": 429, "hits": 22},
    {"ip": "198.51.100.22", "path": "/admin", "kind": "scanner", "status": 403, "hits": 8},
]
_HONEYPOT_WEIGHT = {"env_harvest": 90, "hammer_after_429": 100, "rate_probe": 70,
                    "lfi_probe": 85, "sqli_probe": 85, "scanner": 50,
                    "upload_attempt": 65}

def honeypot_report(entries: list[dict] | None = None) -> dict:
    entries = entries if entries is not None else HONEYPOT_ENTRIES
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    top = max((_HONEYPOT_WEIGHT.get(e["kind"], 10) for e in entries), default=0)
    bursts = sum(1 for e in entries if e["hits"] >= 20)
    severe = any(e["kind"] == "hammer_after_429" and e["hits"] >= 50 for e in entries)
    return {"attempts": len(entries), "classes": counts, "top_threat": top,
            "bursts": bursts, "severe_attack": severe,
            "listener_open": False, "access_granted": False,
            "source": "synthetic TEST-NET-3 logs only (198.51.100.0/24)"}

# --- sandbox-api chamber (merged VERBATIM as sandbox_api.py beside this
# file, sha256 28b3365c0466027079a4985520840f6532d7d9a0c5b4239b840e561f06de54b0)
# + its payload triage: samples are DATA ONLY, never executed, network 0.
try:
    from sandbox_api import detonate_static as _detonate_static
    _CHAMBER_AVAILABLE = True
except Exception:  # pragma: no cover - single-file fallback keeps the lab runnable
    _CHAMBER_AVAILABLE = False

CHAMBER_DANGER_MARKERS = {
    "eval(": "eval_call", "exec(": "exec_call", "os.system": "shell_command",
    "subprocess": "subprocess", "socket.connect": "raw_socket_connect",
    "| sh": "pipe_to_shell", "rm -rf": "destructive_delete",
}
CHAMBER_PAYLOADS = [
    {"name": "rate-config", "data": b'{"env": "linux-cloud", "capture": ["telemetry", "fs-diff"]}'},
    {"name": "limit-report-draft", "data": b'{"title": "weekly limits", "findings": 3, "highest": "LOW"}'},
    {"name": "ioc-list-csv", "data": b'indicator,type\nupdates.example.com,domain\n203.0.113.10,ip\n'},
    {"name": "mask-log-lines", "data": b'INFO boot ok\nINFO pacing ok\nWARN none\n'},
    {"name": "payload-eval-marker", "data": b'{"action": "run", "code": "eval(user_input)"}'},
    {"name": "payload-socket-attempt", "data": b'{"action": "exfil", "code": "socket.connect((host, 4444))"}'},
    {"name": "payload-shell-command", "data": b'{"action": "setup", "code": "os.system(cleanup)"}'},
]

def _analyse_payload(data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    fingerprints = [label for marker, label in CHAMBER_DANGER_MARKERS.items() if marker in text]
    return {"fingerprints": fingerprints,
            "node_count": len(text.splitlines()) or 1,
            "bytes_scanned": len(data),
            "verdict": "HOLD_IN_CHAMBER" if fingerprints else "ALLOW_STATIC_STUDY"}

def chamber_run() -> dict:
    runs = []
    for p in CHAMBER_PAYLOADS:
        if _CHAMBER_AVAILABLE:
            result = _detonate_static(p["name"], p["data"], _analyse_payload)
            report, telemetry = result["report"], result["telemetry"]
            executed = telemetry["executed_sample_code"]
            network = telemetry["network_attempts"]
        else:  # static scan only, still never executed
            report = _analyse_payload(p["data"])
            executed, network = False, 0
        runs.append({"name": p["name"], "verdict": report["verdict"],
                     "fingerprints": report["fingerprints"],
                     "executed_sample_code": executed, "network_attempts": network})
    held = [r for r in runs if r["verdict"] == "HOLD_IN_CHAMBER"]
    return {"chamber": "sandbox-api (verbatim module, static, never executes)",
            "chamber_module_verbatim": _CHAMBER_AVAILABLE,
            "total": len(runs), "allowed": len(runs) - len(held), "held": len(held),
            "all_executed": any(r["executed_sample_code"] for r in runs),
            "total_network_attempts": sum(r["network_attempts"] for r in runs),
            "runs": runs}

# --- Auto-save + resilient reboot.
# Checkpoints are atomic (write temp, rename) and hash-sealed: a
# checkpoint whose hash does not verify is treated as TAMPERED (the
# "being hacked" case) and is never trusted — the runtime restores the
# previous good checkpoint instead. "Reboot" = an in-app restart of the
# mask runtime from the last good state (counters, incident log entry,
# safe mode when the trigger was an attack). It is NOT an OS reboot.
class AutoSave:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "checkpoint.json")
        self.hash_path = os.path.join(directory, "checkpoint.sha256")
        self.prev_path = os.path.join(directory, "checkpoint.prev.json")
        self.prev_hash_path = os.path.join(directory, "checkpoint.prev.sha256")
        self.saves = 0

    @staticmethod
    def _digest(payload: str) -> str:
        return hashlib.sha256(payload.encode()).hexdigest()

    def save(self, state: dict) -> dict:
        payload = json.dumps(state, sort_keys=True, default=str)
        digest = self._digest(payload)
        if os.path.exists(self.path):
            with open(self.path) as fh:
                prev = fh.read()
            with open(self.prev_path, "w") as fh:
                fh.write(prev)
            with open(self.prev_hash_path, "w") as fh:
                fh.write(self._digest(prev))
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)  # atomic
        with open(self.hash_path, "w") as fh:
            fh.write(digest)
        self.saves += 1
        return {"saved": True, "sha256": digest[:16], "saves": self.saves}

    def _load_file(self, path: str, hash_path: str) -> tuple[dict | None, bool]:
        if not os.path.exists(path):
            return None, False
        with open(path) as fh:
            payload = fh.read()
        expected = ""
        if os.path.exists(hash_path):
            with open(hash_path) as fh:
                expected = fh.read().strip()
        ok = bool(expected) and self._digest(payload) == expected
        if not ok:
            return None, True  # present but does not verify = tampered/corrupt
        return json.loads(payload), False

    def load(self) -> dict:
        state, tampered = self._load_file(self.path, self.hash_path)
        if state is not None:
            return {"state": state, "tampered": False, "restored_from": "checkpoint"}
        prev, _ = self._load_file(self.prev_path, self.prev_hash_path)
        if prev is not None:
            return {"state": prev, "tampered": tampered, "restored_from": "checkpoint.prev"}
        return {"state": None, "tampered": tampered, "restored_from": None}

class ResilientRuntime:
    """Auto-save + reboot wrapper around tracker, mask and incident log.

    Triggers: crash/exception -> restore last good + reboot normal.
    Tampered checkpoint or severe HoneypotX attack -> forced incident
    save, restore last good, reboot into SAFE MODE (everything held
    except pacing inside limits) and quarantine flagged sources.
    """
    def __init__(self, autosave: AutoSave, tracker: UnifiedTracker | None = None):
        self.autosave = autosave
        self.tracker = tracker or UnifiedTracker()
        self.universal = UniversalMask(self.tracker.clock)
        self.reboots = 0
        self.safe_mode = False
        self.quarantine: list[str] = []
        self.incidents: list[dict] = []

    def snapshot(self) -> dict:
        return {"analytics": self.tracker.analytics()["global"],
                "records": len(self.tracker.records),
                "reboots": self.reboots, "safe_mode": self.safe_mode,
                "quarantine": list(self.quarantine)}

    def checkpoint(self) -> dict:
        return self.autosave.save(self.snapshot())

    def reboot(self, trigger: str) -> dict:
        loaded = self.autosave.load()
        self.reboots += 1
        attack = trigger in ("tampered_checkpoint", "honeypot_severe")
        if attack:
            self.safe_mode = True
            rep = honeypot_report()
            self.quarantine = sorted({e["ip"] for e in HONEYPOT_ENTRIES
                                      if e["kind"] in ("hammer_after_429", "env_harvest")})
        self.incidents.append({"trigger": trigger, "tampered": loaded["tampered"],
                                "restored_from": loaded["restored_from"],
                                "safe_mode": self.safe_mode, "reboot_number": self.reboots})
        return {"rebooted": True, "trigger": trigger, "tampered": loaded["tampered"],
                "restored_from": loaded["restored_from"], "safe_mode": self.safe_mode,
                "quarantine": list(self.quarantine), "reboots": self.reboots,
                "scope": "in-app mask runtime restart from last good checkpoint (not an OS reboot)"}

    def run_guarded(self, fn) -> dict:
        try:
            result = fn()
            self.checkpoint()
            return {"ok": True, "result": result}
        except Exception as exc:  # crash path: save incident state, reboot, restore
            self.incidents.append({"trigger": "crash", "error": str(exc)})
            return {"ok": False, "error": str(exc), "recovery": self.reboot("crash")}

    def health_check(self) -> dict:
        loaded = self.autosave.load()
        if loaded["tampered"]:
            return self.reboot("tampered_checkpoint")
        rep = honeypot_report()
        if rep["severe_attack"]:
            return self.reboot("honeypot_severe")
        return {"rebooted": False, "healthy": True, "honeypot_top_threat": rep["top_threat"]}

# ------------------------------------------------ catalog ALL + elite
# ------------------------------------------------ scout team (missions)
# The catalog is the everything-layer: the 157 modelled providers are
# catalogued automatically, and every other public API the scouts find
# joins as a catalog entry with an honest limit status:
#   published_modelled  - in PROVIDERS, full limit model (157)
#   catalogued_seed     - known public API, seed entry, scout must verify
#   proposed            - scout-discovered, verified on the wire, NOT yet
#                         applied (owner approves catalog changes)
#   generic_only        - no published limit; universal mask floor applies
CATALOG_CATEGORIES = (
    "Animals", "Anime", "Anti-Malware", "Art & Design", "Authentication",
    "Blockchain", "Books", "Business", "Calendar", "Cloud Storage & File Sharing",
    "Continuous Integration", "Cryptocurrency", "Currency Exchange",
    "Data Validation", "Development", "Dictionaries", "Documents & Productivity",
    "Email", "Entertainment", "Environment", "Events", "Finance",
    "Food & Drinks", "Games & Comics", "Geocoding", "Government", "Health",
    "Jobs", "Machine Learning", "Music", "News", "Open Data",
    "Open Source Projects", "Patent", "Personality", "Phone", "Photos",
    "Programming", "Science & Math", "Security", "Shopping", "Social",
    "Sports & Fitness", "Test Data", "Text Analysis", "Tracking",
    "Transportation", "URL Shorteners", "Vehicle", "Video", "Weather",
)

# Seed entries: well-known public APIs from the public directory world,
# name + category + auth shape only. Limit status is honestly "seed"
# until a scout verifies a published limit and proposes an upgrade.
CATALOG_SEED = [
    ("PokeAPI", "Games & Comics", "none"), ("Dog CEO", "Animals", "none"),
    ("Cat Facts", "Animals", "none"), ("Open Library", "Books", "none"),
    ("REST Countries", "Geocoding", "none"), ("Open-Meteo", "Weather", "none"),
    ("Jikan (MyAnimeList)", "Anime", "none"), ("CoinGecko", "Cryptocurrency", "apiKey"),
    ("Open Exchange Rates", "Currency Exchange", "apiKey"), ("Frankfurter", "Currency Exchange", "none"),
    ("GitHub Gists (public)", "Development", "OAuth"), ("JSONPlaceholder", "Development", "none"),
    ("ReqRes", "Test Data", "none"), ("RandomUser", "Test Data", "none"),
    ("OpenFDA", "Health", "apiKey"), ("Disease.sh", "Health", "none"),
    ("ISS Position (Open Notify)", "Science & Math", "none"), ("SpaceX API (community)", "Science & Math", "none"),
    ("Studio Ghibli", "Entertainment", "none"), ("TVmaze", "Entertainment", "none"),
    ("Lyrics.ovh", "Music", "none"), ("Open Food Facts", "Food & Drinks", "none"),
    ("TheCocktailDB", "Food & Drinks", "none"), ("Open Brewery DB", "Food & Drinks", "none"),
    ("Free Dictionary", "Dictionaries", "none"), ("PoetryDB", "Books", "none"),
    ("Quotable (community)", "Personality", "none"), ("Advice Slip", "Personality", "none"),
    ("Bored API (community)", "Personality", "none"), ("Numbers API (community)", "Science & Math", "none"),
    ("Agify", "Data Validation", "none"), ("Genderize", "Data Validation", "none"),
    ("Nationalize", "Data Validation", "none"), ("IP-API", "Geocoding", "none"),
    ("Zippopotam", "Geocoding", "none"), ("Postman Echo", "Development", "none"),
    ("httpbin", "Development", "none"), ("OpenAQ", "Environment", "apiKey"),
    ("Carbon Interface", "Environment", "apiKey"), ("UK Police Data", "Government", "none"),
    ("Data.gov (catalog)", "Government", "apiKey"), ("World Bank", "Finance", "none"),
    ("FRED (St. Louis Fed)", "Finance", "apiKey"), ("SEC EDGAR (fair access)", "Finance", "none"),
    ("Hacker News (Firebase)", "News", "none"), ("Spaceflight News", "News", "none"),
    ("Football-Data.org", "Sports & Fitness", "apiKey"), ("TheSportsDB", "Sports & Fitness", "apiKey"),
    ("Open Trivia DB", "Games & Comics", "none"), ("RAWG", "Games & Comics", "apiKey"),
    ("Deck of Cards", "Games & Comics", "none"), ("Unsplash Source (deprecated watch)", "Photos", "apiKey"),
    ("Lorem Picsum", "Photos", "none"), ("Shibe Online", "Animals", "none"),
    ("Random Fox", "Animals", "none"), ("CryptoCompare (news)", "Cryptocurrency", "apiKey"),
    ("Blockchain.info", "Blockchain", "none"), ("Mempool.space", "Blockchain", "none"),
    ("Have I Been Pwned", "Security", "apiKey"), ("AbuseIPDB", "Security", "apiKey"),
    ("VirusTotal (public)", "Anti-Malware", "apiKey"), ("urlscan.io", "Security", "apiKey"),
    ("Clearbit Logo", "Business", "none"), ("Hunter (email verify)", "Email", "apiKey"),
    ("Mailboxlayer", "Email", "apiKey"), ("EVA (email validation)", "Data Validation", "none"),
    ("Open Calendar (Nager.Date)", "Calendar", "none"), ("Calendarific", "Calendar", "apiKey"),
    ("RemoteOK Jobs", "Jobs", "none"), ("Arbeitnow", "Jobs", "none"),
    ("USAJOBS", "Jobs", "apiKey"), ("Google Books", "Books", "apiKey"),
    ("OpenAI-compatible gateways (generic)", "Machine Learning", "apiKey"),
    ("Cohere (trial sandbox)", "Machine Learning", "apiKey"),
    ("TMDB", "Video", "apiKey"), ("OMDb", "Video", "apiKey"),
    ("YouTube Data (quota units)", "Video", "apiKey"), ("Vimeo (public oEmbed)", "Video", "none"),
    ("Last.fm", "Music", "apiKey"), ("MusicBrainz", "Music", "none"),
    ("OpenStreetMap Nominatim", "Geocoding", "none"), ("OpenSea (watch)", "Blockchain", "apiKey"),
    ("DHL / carrier tracking (generic)", "Tracking", "apiKey"), ("FlightAware AeroAPI (watch)", "Transportation", "apiKey"),
    ("OpenSky Network", "Transportation", "none"), ("TfL (Transport for London)", "Transportation", "apiKey"),
    ("Digitransit", "Transportation", "none"), ("NHTSA Vehicles", "Vehicle", "none"),
    ("CleanURI", "URL Shorteners", "none"), ("is.gd", "URL Shorteners", "none"),
    ("USPTO (watch)", "Patent", "apiKey"), ("EPO Open Patent (watch)", "Patent", "OAuth"),
    ("NumVerify", "Phone", "apiKey"), ("OpenPhone (watch)", "Phone", "apiKey"),
    ("Shodan (watch)", "Security", "apiKey"), ("Censys (watch)", "Security", "apiKey"),
    ("Art Institute of Chicago", "Art & Design", "none"), ("Met Museum", "Art & Design", "none"),
    ("Rijksmuseum", "Art & Design", "apiKey"), ("Dribbble (watch)", "Art & Design", "OAuth"),
    ("Cloudinary (demo cloud)", "Cloud Storage & File Sharing", "apiKey"),
    ("Firebase (client quotas)", "Cloud Storage & File Sharing", "apiKey"),
    ("Travis CI (watch)", "Continuous Integration", "apiKey"), ("CircleCI (watch)", "Continuous Integration", "apiKey"),
    ("App Store Connect (watch)", "Shopping", "apiKey"), ("Fake Store API", "Shopping", "none"),
    ("DummyJSON", "Test Data", "none"), ("OpenWeather (One Call watch)", "Weather", "apiKey"),
    ("Weatherstack", "Weather", "apiKey"), ("Semantic Scholar", "Open Data", "apiKey"),
    ("Europeana", "Open Data", "apiKey"), ("GDELT (watch)", "Open Data", "none"),
    ("Libraries.io", "Open Source Projects", "apiKey"), ("Open Collective", "Open Source Projects", "none"),
    ("Eventbrite (watch)", "Events", "OAuth"), ("Ticketmaster Discovery", "Events", "apiKey"),
    ("PredictHQ (watch)", "Events", "OAuth"), ("SeatGeek", "Events", "apiKey"),
    ("LanguageLayer", "Text Analysis", "apiKey"), ("MeaningCloud (watch)", "Text Analysis", "apiKey"),
    ("Aylien (watch)", "Text Analysis", "apiKey"), ("IMDb (community watch)", "Entertainment", "apiKey"),
    ("SuperTokens", "Authentication", "none"), ("Frontegg (watch)", "Authentication", "apiKey"),
    ("DocuSeal", "Documents & Productivity", "apiKey"), ("PDFBlade (watch)", "Documents & Productivity", "apiKey"),
    ("Judge0", "Programming", "none"), ("Piston (emkc)", "Programming", "none"),
    ("PeerTube", "Social", "none"), ("Lemmy (watch)", "Social", "apiKey"),
]

class APICatalog:
    """Every API in one catalog: modelled, seeded, proposed, generic."""
    def __init__(self):
        self.entries: dict[str, dict] = {}
        for key, p in PROVIDERS.items():
            self.add(p.name, "Development" if p.kind == "api" else "Machine Learning",
                     "varies", "published_modelled", provider_key=key)
        for name, category, auth in CATALOG_SEED:
            self.add(name, category, auth, "catalogued_seed")

    def add(self, name: str, category: str, auth: str, limit_status: str,
            provider_key: str | None = None) -> dict:
        slug = name.lower().replace(" ", "_")
        if slug in self.entries:
            return self.entries[slug]
        entry = {"name": name, "category": category, "auth": auth,
                 "limit_status": limit_status, "provider_key": provider_key}
        self.entries[slug] = entry
        return entry

    def summary(self) -> dict:
        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for e in self.entries.values():
            by_status[e["limit_status"]] = by_status.get(e["limit_status"], 0) + 1
            by_category[e["category"]] = by_category.get(e["category"], 0) + 1
        return {"total_catalogued": len(self.entries),
                "categories_covered": len(by_category),
                "categories_total": len(CATALOG_CATEGORIES),
                "by_status": by_status, "by_category": by_category,
                "uncovered_categories": [c for c in CATALOG_CATEGORIES if c not in by_category]}

# Synthetic discovery wire — labelled fixtures of NEW APIs the elite
# team "finds" on patrol. Not real launches; the mission workflow is
# what's real. Verified candidates are PROPOSED, never auto-applied.
DISCOVERY_WIRE = [
    {"name": "SYNTHETIC FIXTURE: Aurora Weather Grid", "category": "Weather",
     "auth": "apiKey", "published_limit": "600 requests/minute", "source": "synthetic wire"},
    {"name": "SYNTHETIC FIXTURE: LedgerLane Payments", "category": "Finance",
     "auth": "apiKey", "published_limit": None, "source": "synthetic wire"},
    {"name": "SYNTHETIC FIXTURE: PetGraph Social", "category": "Social",
     "auth": "OAuth", "published_limit": "300 requests per 5 minutes", "source": "synthetic wire"},
    {"name": "SYNTHETIC FIXTURE: Orbital Pass Tracker", "category": "Science & Math",
     "auth": "none", "published_limit": "1 request/second", "source": "synthetic wire"},
    {"name": "SYNTHETIC FIXTURE: Mystery Blackbox API", "category": "Development",
     "auth": "unknown", "published_limit": None, "source": "synthetic wire"},
]

MISSION_WORKFLOW = [
    ("BRIEF", "Commander", "mission scope: categories + regions in balance; success = verified catalog proposals"),
    ("DISCOVER", "Scouts", "fan out across directory, changelog and status wires; collect candidate APIs"),
    ("VERIFY", "Verifiers", "confirm the candidate exists, its auth shape, and whether a rate limit is PUBLISHED"),
    ("CATALOG", "Cataloguers", "dedupe against the catalog; draft the entry with honest limit status"),
    ("WATCH", "Watchers", "attach rules/news/updates watches so future limit changes are caught"),
    ("DECIDE", "Argon 4 + Muse council", "council reviews proposals; HOLD-gated items never advance"),
    ("REPORT", "Analysts", "mission report: found / verified / proposed / rejected, with balance stats"),
]

class EliteScoutTeam:
    """250,000 VIRTUAL bots as a balanced elite team.

    Balance rule: every provider and every catalog category gets a
    shard; no shard may exceed 2x the smallest shard, and mission roles
    are staffed in fixed proportion (Commander 1%, Scouts 39%,
    Verifiers 25%, Cataloguers 15%, Watchers 15%, Analysts 5%).
    Live fetching stays HOLD-gated — missions run on the wire given.
    """
    VIRTUAL_BOTS = 250_000
    ROLE_SHARES = {"Commander": 0.01, "Scouts": 0.39, "Verifiers": 0.25,
                   "Cataloguers": 0.15, "Watchers": 0.15, "Analysts": 0.05}

    def __init__(self, catalog: APICatalog | None = None):
        self.catalog = catalog or APICatalog()
        self.missions = 0
        self.proposals: list[dict] = []

    def role_staffing(self) -> dict:
        return {role: int(self.VIRTUAL_BOTS * share)
                for role, share in self.ROLE_SHARES.items()}

    def shard_map(self) -> dict:
        targets = list(PROVIDERS) + [f"category:{c}" for c in CATALOG_CATEGORIES]
        base = self.VIRTUAL_BOTS // len(targets)
        shards = {t: base for t in targets}
        for t in targets[: self.VIRTUAL_BOTS - base * len(targets)]:
            shards[t] += 1
        sizes = list(shards.values())
        return {"targets": len(targets), "bots_per_shard_min": min(sizes),
                "bots_per_shard_max": max(sizes),
                "balanced": max(sizes) <= 2 * min(sizes),
                "total_bots_virtual": sum(sizes)}

    def workflow(self) -> list[dict]:
        return [{"stage": s, "owner": o, "job": j} for s, o, j in MISSION_WORKFLOW]

    def run_mission(self, wire: list[dict] | None = None) -> dict:
        wire = DISCOVERY_WIRE if wire is None else wire
        self.missions += 1
        found, verified, proposed, rejected = len(wire), 0, 0, []
        for cand in wire:
            if cand.get("auth") in (None, "unknown") and not cand.get("published_limit"):
                rejected.append({"name": cand["name"], "reason": "unverifiable: no auth shape, no published limit"})
                continue
            verified += 1
            entry = {"name": cand["name"], "category": cand["category"],
                     "auth": cand["auth"],
                     "limit_status": "proposed" if cand.get("published_limit") else "generic_only",
                     "published_limit": cand.get("published_limit"),
                     "status": "PROPOSED — not applied; owner approves catalog changes",
                     "watches_attached": list(WATCH_KINDS)}
            self.proposals.append(entry)
            proposed += 1
        return {"mission": self.missions, "workflow": [s for s, _, _ in MISSION_WORKFLOW],
                "found": found, "verified": verified, "proposed": proposed,
                "rejected": rejected, "balance": self.shard_map(),
                "staffing": self.role_staffing(), "live_calls": 0,
                "catalog_after": self.catalog.summary()["total_catalogued"]}

# ------------------------------------------------------- auto-updates
# The registry updates itself — safely. Scout findings flow through a
# policy gate: a change is AUTO-APPLIED only when it is a documented
# limit-value change for an already-registered provider, seen on the
# wire with a concrete new value, and the council does not tighten or
# hold it. Everything else — new providers, removals, auth or secret
# shape changes, conflicting or value-less reports — stays PROPOSED
# for the owner. Every applied update: (1) autosave checkpoint FIRST
# (the rollback point), (2) bump the registry version, (3) write a
# changelog entry. Rollback restores the pre-update snapshot values.
# Scope is DATA (registry values) only — code updates are never
# auto-applied; those stay owner-reviewed, always.

@dataclass
class UpdateRecord:
    provider: str
    label: str
    old: str | None
    new: str
    action: str            # AUTO_APPLIED | PROPOSED | HELD
    reason: str
    version: str

class AutoUpdater:
    AUTO_APPLY_REASONS = "documented value change, registered provider, council PROCEED"
    def __init__(self, autosave: AutoSave | None = None, council: Council | None = None):
        self.autosave = autosave
        self.council = council or Council(UnifiedTracker())
        self.version = "2026.11.13-registry.0"
        self.changelog: list[UpdateRecord] = []
        self.pending: list[UpdateRecord] = []
        self._snapshots: list[dict] = []   # rollback stack of {provider,label,old}
        self.applied_values: dict[tuple, str] = {}   # live overlay over the frozen registry
        self.enabled = True

    def _bump(self) -> str:
        n = int(self.version.rsplit(".", 1)[1]) + 1
        self.version = self.version.rsplit(".", 1)[0] + f".{n}"
        return self.version

    def classify(self, finding: dict) -> UpdateRecord:
        provider, change = finding.get("provider"), finding.get("change")
        if not change or " -> " not in str(change):
            return UpdateRecord(provider, "", None, "", "PROPOSED",
                                "no concrete value change on the wire", self.version)
        label, rest = str(change).split(":", 1)
        old, new = (x.strip() for x in rest.split(" -> ", 1))
        label = label.strip()
        if provider not in PROVIDERS:
            return UpdateRecord(provider, label, old, new, "PROPOSED",
                                "unregistered provider: catalog proposal path", self.version)
        verdict = self.council.decide("pace_inside_limits").verdict
        if verdict == "HOLD":
            return UpdateRecord(provider, label, old, new, "HELD",
                                "council HOLD stands over auto-update", self.version)
        return UpdateRecord(provider, label, old, new, "AUTO_APPLIED",
                            self.AUTO_APPLY_REASONS, self.version)

    def apply_wire(self, wire: list[dict] | None = None) -> dict:
        if not self.enabled:
            return {"enabled": False, "applied": 0, "proposed": 0, "held": 0}
        findings = ScoutSwarm().cycle(wire)
        applied = proposed = held = 0
        for f in findings:
            if not f.get("change"):
                continue
            rec = self.classify(f)
            if rec.action == "AUTO_APPLIED":
                if self.autosave is not None:
                    self.autosave.save({"pre_update": rec.provider,
                                        "version": self.version})
                self._snapshots.append({"provider": rec.provider, "label": rec.label,
                                        "old": rec.old})
                self.applied_values[(rec.provider, rec.label)] = rec.new
                rec.version = self._bump()
                applied += 1
            elif rec.action == "HELD":
                self.pending.append(rec)
                held += 1
            else:
                self.pending.append(rec)
                proposed += 1
            self.changelog.append(rec)
        return {"enabled": True, "applied": applied, "proposed": proposed,
                "held": held, "version": self.version,
                "changelog_entries": len(self.changelog)}

    def rollback_last(self) -> dict:
        if not self._snapshots:
            return {"rolled_back": False, "reason": "nothing applied yet"}
        snap = self._snapshots.pop()
        self.applied_values.pop((snap["provider"], snap["label"]), None)
        return {"rolled_back": True, "provider": snap["provider"],
                "label": snap["label"], "restored_value": snap["old"],
                "note": "registry value restored from the pre-update checkpoint stack"}

    def effective_snapshot(self, provider: str) -> dict:
        """Registry snapshot with auto-applied overlay values on top."""
        base = dict(PROVIDERS[provider].snapshot)
        for (prov, label), new in self.applied_values.items():
            if prov == provider:
                base[label] = new
        return base

class AutoUpdateScheduler:
    """Runs the auto-update cycle on an interval (injectable clock)."""
    def __init__(self, updater: AutoUpdater, clock: Clock, interval_seconds: float = 3600.0):
        self.updater, self.clock, self.interval = updater, clock, interval_seconds
        self.last_run: float | None = None
        self.runs = 0

    def due(self) -> bool:
        return self.last_run is None or (self.clock.now() - self.last_run) >= self.interval

    def tick(self, wire: list[dict] | None = None) -> dict:
        if not self.due():
            return {"ran": False, "reason": "interval not reached",
                    "next_in_seconds": self.interval - (self.clock.now() - (self.last_run or 0))}
        self.last_run = self.clock.now()
        self.runs += 1
        result = self.updater.apply_wire(wire)
        return {"ran": True, "run": self.runs, **result}

# ------------------------------------------------------------------- checks

def run_checks() -> tuple[int, int, list[str]]:
    lines, passed = [], 0
    def check(name, cond):
        nonlocal passed
        lines.append(f"{'PASS' if cond else 'FAIL'}  {name}")
        passed += 1 if cond else 0

    check("registry holds all 157 systems", len(PROVIDERS) == 157)
    check("registry snapshot is dated", all(p.snapshot for p in PROVIDERS.values()))

    c = Clock()
    tb = TokenBucket(5, 1.0, c)
    check("token bucket allows burst of capacity", all(tb.allow().allowed for _ in range(5)))
    check("token bucket then refuses", not tb.allow().allowed)
    c.advance(2)
    check("token bucket refills over time", tb.allow().allowed)

    fw = FixedWindow(3, 60, Clock())
    check("fixed window caps at limit", [fw.allow().allowed for _ in range(4)] == [True, True, True, False])

    gh = parse_headers({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999",
                        "X-RateLimit-Reset": "1760000000", "X-RateLimit-Resource": "core"})
    check("github headers parse", gh.provider == "github_rest" and gh.remaining == 4999 and gh.resource == "core")
    oa = parse_headers({"x-ratelimit-limit-requests": "5000", "x-ratelimit-remaining-tokens": "999000"})
    check("openai/muse header family parses", oa.limit_requests == 5000 and oa.remaining_tokens == 999000)
    an = parse_headers({"anthropic-ratelimit-requests-limit": "4000",
                        "anthropic-ratelimit-input-tokens-remaining": "1900000"})
    check("anthropic headers parse", an.provider == "claude_api" and an.limit_requests == 4000)
    check("retry-after seconds parse", parse_retry_after({"Retry-After": "42"}) == 42.0)
    check("fingerprint gemini body", fingerprint_provider({}, "RESOURCE_EXHAUSTED") == "gemini_api")
    check("fingerprint anthropic headers", fingerprint_provider({"anthropic-ratelimit-requests-limit": "1"}) == "claude_api")
    check("classify github secondary", classify_429("github_rest", {}, "You have exceeded a secondary rate limit") == "secondary_burst_limit")

    sim = SyntheticProvider("github_rest", Clock())
    results = [sim.call(mutation=True) for _ in range(4)]  # 5pts each, cap 18/min
    check("github simulator: mutation points trip secondary", results[3].allowed is False and results[3].metric_hit == "secondary_points_per_minute")
    sim = SyntheticProvider("google_flow", Clock())
    flow = [sim.call(kind="quality") for _ in range(11)]  # 50 daily covers 0 quality; monthly 1000 covers 10
    check("flow simulator: quality drains monthly pool", flow[9].allowed and not flow[10].allowed)
    sim = SyntheticProvider("claude_api", Clock())
    big = sim.call(tokens_in=10, tokens_out=500)  # OTPM bucket scaled to 400
    check("claude simulator: output meter is separate", (not big.allowed) and big.metric_hit == "output_tokens_per_minute")
    check("cache accounting differs by provider",
          effective_input_tokens(1000, 400, "claude_api") == 600 and effective_input_tokens(1000, 400, "openai_api") == 1000)

    mask = RuntimeMask(TokenBucket(2, 0.1, Clock()), Clock())
    check("mask sends inside budget", mask.before_request().outcome == "SENT")
    m2 = RuntimeMask(TokenBucket(100, 100, Clock()), Clock(), max_consecutive_429=2)
    m2.on_response(429, {"Retry-After": "30"}, "github_rest")
    r = m2.on_response(429, {"Retry-After": "30"}, "github_rest")
    check("mask opens circuit after repeated 429", r.outcome == "CIRCUIT_OPEN")
    m3 = RuntimeMask(TokenBucket(100, 100, Clock()), Clock())
    check("mask honours retry-after", m3.on_response(429, {"Retry-After": "7"}, "openai_api").wait_seconds == 7.0)
    check("gate holds key rotation", gate("rotate_keys_to_evade")[0] == "HOLD")
    check("gate proceeds for pacing", gate("pace_inside_limits")[0] == "PROCEED")

    check("forecast exhaustion", forecast_exhaustion(100, 25) == 4.0)
    check("alert thresholds", alert_level(4, 100) == "critical" and alert_level(60, 100) == "ok")
    check("what-if burst is offline and bounded", what_if_burst("gemini_api", 6)["limited"] >= 1)
    check("tier math openai", tier_upgrade_math("openai_api", 40)["next_tier"] == "Launch")
    check("20 suggestions catalogued", len(SUGGESTIONS_20) == 20 and all(s[2] == "IMPLEMENTED" for s in SUGGESTIONS_20))
    check("reset calendar covers flow", "daily" in reset_calendar("google_flow"))
    check("bucket keys isolate muse by team", bucket_key("muse_model_api", "muse-spark-1.3", team="team-a") != bucket_key("muse_model_api", "muse-spark-1.3", team="team-b"))

    tr = UnifiedTracker()
    for i in range(4):
        tr.clock.advance(15)
        tr.record("openai_api", "gpt-test", 200, latency_ms=100 + i * 50,
                  bytes_in=1000, bytes_out=2000, tokens_in=100, tokens_out=50)
    tr.record("openai_api", "gpt-test", 429, latency_ms=10, error_kind="rate_limit")
    tr.record("google_flow", "veo-lite", 200, latency_ms=8000, credits=10)
    ga = tr.analytics()["global"]
    check("tracker rolls all APIs into one", ga["calls"] == 6 and ga["tokens_total"] == 600)
    check("tracker full metrics: speed + size", ga["latency_ms_p95"] >= 200 and ga["bytes_total"] == 12000 and ga["data_size"].endswith("KB"))
    check("tracker rankings work", tr.analytics()["rankings"]["most_calls"][0] == "openai_api")
    check("check_all covers 157 providers in one table", len(tr.check_all()) == 157 and "openai_api" in tr.render_table())
    check("tracker rejects unregistered provider", _raises(tr))

    council = Council(tr)
    d1 = council.decide("pace_inside_limits")
    check("council proceeds on healthy pacing", d1.verdict in ("PROCEED", "TIGHTEN"))
    d2 = council.decide("rotate_keys_to_evade", stakes="deploy")
    check("argon HOLD stands, muse cannot override", d2.verdict == "HOLD" and len(d2.muse_advice) == 4)
    check("argon trace is hashed, tier by stakes", bool(d2.argon_trace["trace_hash"]) and d2.argon_trace["tier"] == 32768)
    proj = council.manage_project(["pace_inside_limits", "export_policy"], stakes="project")
    check("council manages full project together", proj["steps"] == 2 and proj["managed_by"] == ["Gemini Argon 4 Extended Thinking", "Muse"])

    check("50 added providers join the 7 core", len(MORE_PROVIDERS) == 50 and all(k in PROVIDERS for k in CORE_PROVIDERS))
    check("next 100 published-limit providers join", len(MORE_PROVIDERS_100) == 100 and all(k in PROVIDERS for k in MORE_PROVIDERS_100))
    check("watch registry covers rules+news+updates x157", len(WATCH_SOURCES) == 157 * 3 and {w["kind"] for w in WATCH_SOURCES} == {"rules", "news", "updates"})
    ai = LocalAI(tr)
    brief = ai.brief("openai_api")
    check("local A.I fuses SLM+LLM+ML+NNL, authority none", brief["authority"].startswith("none") and len(brief["models"]) == 4 and bool(brief["slm_summary"]) and bool(brief["llm_briefing"]))
    check("LLM drafting is deterministic per seed", ai.llm.generate("rate limits", 12) == ai.llm.generate("rate limits", 12))
    good = {"error_rate": 0.0, "limit_rate": 0.0, "latency_ms_p95": 120, "success_rate": 1.0}
    bad = {"error_rate": 0.7, "limit_rate": 0.6, "latency_ms_p95": 4800, "success_rate": 0.3}
    check("ML classifies healthy vs critical", ai.ml.classify(good)[0] == "ok" and ai.ml.classify(bad)[0] == "critical")
    check("NNL risk orders bad above good", 0.0 <= ai.nnl.risk(good) < ai.nnl.risk(bad) <= 1.0)
    swarm = ScoutSwarm(ai)
    check("scout fleet is 250,000 virtual bots", swarm.status()["bots_virtual"] == 250_000 and swarm.status()["live_calls"] == 0)
    finds = swarm.cycle(SYNTHETIC_WIRE)
    check("scouts detect a rules change on the wire", any(f["change"] and "300 RPD" in f["change"] for f in finds))
    check("scout findings carry impact + briefing", all(f["impact"] in ("high", "medium", "low") and f["briefing"] for f in finds))
    check("live scout fetch is HOLD-gated", gate("live_scout_fetch")[0] == "HOLD" and swarm.fetch_live("github_rest")[0] == "HOLD")

    um = UniversalMask()
    check("universal mask fits registered + unknown", um.fit("github_rest") == "registry" and um.fit("some_new_api") == "generic_learned")
    um.after("some_new_api", 200, {"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "99"})
    check("universal mask learns unknown API from headers", DYNAMIC_PROVIDERS["some_new_api"]["limit_requests"] == 100.0)
    check("universal coverage is one-size-fits-all", um.coverage()["registered_providers"] == 157)

    hp = honeypot_report()
    check("honeypotx merged: log-only, no listener", hp["listener_open"] is False and hp["access_granted"] is False and hp["attempts"] == 12)
    check("honeypotx classes + top threat", hp["classes"].get("scanner") == 3 and hp["top_threat"] == 100 and hp["severe_attack"] is True)

    ch = chamber_run()
    check("sandbox-api chamber merged verbatim", ch["chamber_module_verbatim"] is True)
    check("chamber never executes, network zero", ch["all_executed"] is False and ch["total_network_attempts"] == 0)
    check("chamber triage 4 allowed / 3 held", ch["allowed"] == 4 and ch["held"] == 3)

    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        rt = ResilientRuntime(AutoSave(td))
        rt.tracker.record("github_rest", "rest", 200, latency_ms=42)
        saved = rt.checkpoint()
        check("autosave writes hash-sealed checkpoint", saved["saved"] is True)
        ok_load = rt.autosave.load()
        check("autosave loads verified state", ok_load["tampered"] is False and ok_load["state"]["records"] == 1)
        with open(rt.autosave.path, "a") as fh:
            fh.write("TAMPERED")
        rb = rt.health_check()
        check("tampered checkpoint -> reboot, never trusted", rb["rebooted"] is True and rb["tampered"] is True and rb["trigger"] == "tampered_checkpoint")
        rt2 = ResilientRuntime(AutoSave(td + "b"))
        crashed = rt2.run_guarded(lambda: 1 / 0)
        check("crash -> auto reboot from last good", crashed["ok"] is False and crashed["recovery"]["rebooted"] is True)
        rt3 = ResilientRuntime(AutoSave(td + "c"))
        rt3.checkpoint()
        atk = rt3.reboot("honeypot_severe")
        check("hack attack -> safe mode + quarantine", atk["safe_mode"] is True and len(atk["quarantine"]) >= 2)

    cat = APICatalog()
    cs = cat.summary()
    check("catalog holds modelled + seed together", cs["by_status"]["published_modelled"] == 157 and cs["by_status"]["catalogued_seed"] >= 100)
    check("catalog spans the public category tree", cs["categories_covered"] == 51 and cs["categories_total"] == len(CATALOG_CATEGORIES))
    team = EliteScoutTeam(cat)
    sm = team.shard_map()
    check("elite team is balanced", sm["balanced"] is True and sm["total_bots_virtual"] == 250_000 and sm["targets"] == 157 + len(CATALOG_CATEGORIES))
    check("role staffing sums to the swarm", abs(sum(team.role_staffing().values()) - 250_000) <= len(team.role_staffing()))
    mission = team.run_mission()
    check("mission workflow runs end to end", mission["workflow"] == [s for s, _, _ in MISSION_WORKFLOW] and mission["found"] == 5)
    check("mission proposes verified, rejects blackbox", mission["proposed"] == 4 and len(mission["rejected"]) == 1 and mission["live_calls"] == 0)
    check("proposals are not auto-applied", all(p["status"].startswith("PROPOSED") for p in team.proposals) and cat.summary()["total_catalogued"] == cs["total_catalogued"])

    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as td:
        up = AutoUpdater(AutoSave(td + "u"))
        res = up.apply_wire()
        check("auto-update applies documented value change", res["applied"] == 1 and res["version"] != "2026.11.13-registry.0")
        check("auto-applied value lives in the overlay", up.effective_snapshot("gemini_api")["free_2.5_flash"].endswith("300 RPD"))
        check("auto-update wrote a changelog", len(up.changelog) == 1 and up.changelog[0].action == "AUTO_APPLIED")
        rb = up.rollback_last()
        check("auto-update rolls back to the old value", rb["rolled_back"] is True and rb["restored_value"].endswith("250 RPD") and up.effective_snapshot("gemini_api")["free_2.5_flash"].endswith("250 RPD"))
        sched = AutoUpdateScheduler(AutoUpdater(), Clock(), interval_seconds=60)
        r1 = sched.tick([])   # empty wire: runs, nothing to apply
        r2 = sched.tick([])
        check("scheduler runs on interval, not every tick", r1["ran"] is True and r2["ran"] is False)
        sched.clock.advance(61)
        check("scheduler runs again after interval", sched.tick([])["ran"] is True)
        up2 = AutoUpdater()
        up2.enabled = False
        check("auto-update can be switched off", up2.apply_wire()["enabled"] is False)
    return passed, len(lines), lines

def _raises(tr) -> bool:
    try:
        tr.record("not_a_provider")
        return False
    except ValueError:
        return True

def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "providers":
        print(json.dumps({k: asdict(v) for k, v in PROVIDERS.items()}, indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "tracker":
        tr = UnifiedTracker()
        for prov, model in (("github_rest", "rest"), ("gemini_api", "gemini-2.5-flash"),
                            ("muse_model_api", "muse-spark-1.3"), ("openai_api", "gpt-test"),
                            ("claude_api", "claude-sonnet")):
            tr.record(prov, model, 200, latency_ms=120, bytes_in=800, bytes_out=1600,
                      tokens_in=90, tokens_out=40)
        tr.record("google_flow", "veo-lite", 200, latency_ms=8000, credits=10)
        print(tr.render_table())
        print(json.dumps(tr.analytics()["rankings"], indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "council":
        tr = UnifiedTracker()
        print(json.dumps(Council(tr).manage_project(
            ["pace_inside_limits", "rotate_keys_to_evade"]), indent=2, default=str))
        return 0
    if len(argv) > 1 and argv[1] == "scouts":
        swarm = ScoutSwarm()
        print(json.dumps({"status": swarm.status(), "findings": swarm.cycle()}, indent=2))
        return 0
    if len(argv) > 2 and argv[1] == "ai":
        print(json.dumps(LocalAI(UnifiedTracker()).brief(argv[2]), indent=2, default=str))
        return 0
    if len(argv) > 1 and argv[1] == "universal":
        um = UniversalMask()
        print(json.dumps(um.coverage(), indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "honeypot":
        print(json.dumps(honeypot_report(), indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "chamber":
        print(json.dumps(chamber_run(), indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "resilience":
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            rt = ResilientRuntime(AutoSave(td))
            rt.tracker.record("github_rest", "rest", 200, latency_ms=42)
            print(json.dumps({"checkpoint": rt.checkpoint(),
                              "health": rt.health_check()}, indent=2, default=str))
        return 0
    if len(argv) > 1 and argv[1] == "catalog":
        print(json.dumps(APICatalog().summary(), indent=2))
        return 0
    if len(argv) > 1 and argv[1] == "elite":
        team = EliteScoutTeam()
        print(json.dumps({"staffing": team.role_staffing(), "balance": team.shard_map(),
                          "workflow": team.workflow(), "mission": team.run_mission()},
                         indent=2, default=str))
        return 0
    if len(argv) > 1 and argv[1] == "autoupdate":
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            up = AutoUpdater(AutoSave(td))
            print(json.dumps({"run": up.apply_wire(),
                              "effective_gemini": up.effective_snapshot("gemini_api"),
                              "rollback": up.rollback_last()}, indent=2, default=str))
        return 0
    if len(argv) > 2 and argv[1] == "simulate":
        n = int(argv[2 + 1]) if len(argv) > 3 else 6
        print(json.dumps(what_if_burst(argv[2], n), indent=2))
        return 0
    print("RATE-LIMIT-MASK — educational rate-limit reverse-engineering lab")
    print(f"providers: {len(PROVIDERS)} ({len(CORE_PROVIDERS)} core + {len(MORE_PROVIDERS)} + {len(MORE_PROVIDERS_100)} published-limit)  |  snapshot: {SNAPSHOT_DATE}  |  mode: synthetic only, no live calls")
    passed, total, lines = run_checks()
    for line in lines:
        print(line)
    print(f"CHECKS {passed}/{total} PASS")
    return 0 if passed == total else 1

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
