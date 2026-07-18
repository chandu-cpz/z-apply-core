from __future__ import annotations

# Static router exclusions for models below the 120B routing floor, specialized
# non-agent models, or retired endpoints. Larger eligible models are selected by
# the NIM router from its live catalog and runtime history.
BLOCKED_MODEL_IDS_BELOW_120B: tuple[str, ...] = (
    "bytedance/seed-oss-36b-instruct",
    "google/codegemma-7b",
    "google/gemma-2-2b-it",
    "google/gemma-3-12b-it",
    "google/gemma-3-27b-it",
    "google/gemma-3-4b-it",
    "google/gemma-3n-e2b-it",
    "google/gemma-3n-e4b-it",
    "google/gemma-4-31b-it",
    "google/paligemma",
    "meta/llama-3.1-8b-instruct",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-3.2-3b-instruct",
    "meta/llama-guard-4-12b",
    "microsoft/phi-3-vision-128k-instruct",
    "microsoft/phi-4-mini-instruct",
    "microsoft/phi-4-multimodal-instruct",
    "mistralai/mistral-small-3.1-24b-instruct-2503",
    "mistralai/codestral-22b-instruct-v0.1",
    "mistralai/mathstral-7b-v0.1",
    "mistralai/ministral-14b-instruct-2512",
    "mistralai/ministral-3-14b-instruct-2512",
    "mistralai/mistral-7b-instruct-v0.3",
    "mistralai/mistral-nemotron",
    "nvidia/llama-3.1-nemoguard-8b-content-safety",
    "nvidia/llama-3.1-nemoguard-8b-topic-control",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nvidia/llama-3.1-nemotron-nano-4b-v1.1",
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/nemoretriever-parse",
    "nvidia/nemotron-mini-4b-instruct",
    "nvidia/nemotron-nano-12b-v2-vl",
    "nvidia/nvclip",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "nvidia/nemotron-3-nano-30b-a3b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3-next-80b-a3b-thinking",
    "qwen/qwq-32b",
    "upstage/solar-10.7b-instruct",
    "zyphra/zamba2-7b-instruct",
    "nv-mistralai/mistral-nemo-12b-instruct",
    "ibm/granite-3.3-8b-instruct",
)

# NIM models must satisfy the 120B floor (except the user-approved Laguna
# exception) and pass a local native-tool-call probe before they may drive a
# job application. Keep this at the router-policy boundary; agents continue to
# request capabilities rather than model IDs.
VERIFIED_LARGE_TOOL_MODEL_IDS: tuple[str, ...] = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "z-ai/glm-5.2",
    "minimaxai/minimax-m3",
    "minimaxai/minimax-m2.7",
    "deepseek-ai/deepseek-v4-flash",
    "stepfun-ai/step-3.5-flash",
    "stepfun-ai/step-3.7-flash",
    "mistralai/mistral-medium-3.5-128b",
    "qwen/qwen3.5-122b-a10b",
)

# NVIDIA's free-tier catalog can report unknown or false tool capability for
# endpoints that have passed the native tool probe above. These explicit facts
# override only that stale provider metadata; they do not permit unprobed
# models into the fixed application pool.
PROBED_TOOL_CAPABILITY_OVERRIDES: dict[str, dict[str, bool]] = {
    "z-ai/glm-5.2": {"tools": True},
    "minimaxai/minimax-m3": {"tools": True},
    "minimaxai/minimax-m2.7": {"tools": True},
}
