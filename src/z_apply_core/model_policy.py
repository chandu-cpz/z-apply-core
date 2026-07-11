from __future__ import annotations

# Static router exclusions for non-deprecated models returned by
# ``ChatNVIDIA.get_available_models()`` that have fewer than 30 billion total
# parameters. Keep this explicit because the discovery metadata does not expose
# parameter counts.
BANNED_MODEL_IDS_UNDER_30B: tuple[str, ...] = (
    "google/codegemma-7b",
    "google/gemma-2-2b-it",
    "google/gemma-3-12b-it",
    "google/gemma-3-27b-it",
    "google/gemma-3-4b-it",
    "google/gemma-3n-e2b-it",
    "google/gemma-3n-e4b-it",
    "google/paligemma",
    "meta/llama-3.1-8b-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-3.2-3b-instruct",
    "meta/llama-guard-4-12b",
    "microsoft/phi-3-vision-128k-instruct",
    "microsoft/phi-4-mini-instruct",
    "microsoft/phi-4-multimodal-instruct",
    "mistralai/codestral-22b-instruct-v0.1",
    "mistralai/mathstral-7b-v0.1",
    "mistralai/ministral-14b-instruct-2512",
    "mistralai/ministral-3-14b-instruct-2512",
    "mistralai/mistral-7b-instruct-v0.3",
    "mistralai/mistral-nemotron",
    "nvidia/llama-3.1-nemoguard-8b-content-safety",
    "nvidia/llama-3.1-nemoguard-8b-topic-control",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
    "nvidia/nemoretriever-parse",
    "nvidia/nemotron-mini-4b-instruct",
    "nvidia/nemotron-nano-12b-v2-vl",
    "nvidia/nvclip",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3-nano-30b-a3b",
    "upstage/solar-10.7b-instruct",
    "zyphra/zamba2-7b-instruct",
)
