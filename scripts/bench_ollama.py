"""Compare local-model generation speed on CPU vs GPU (Metal).

    PYTHONPATH=. .venv/bin/python scripts/bench_ollama.py

On this Mac the CPU was ~6x faster than the GPU (22 vs 3.5 tokens/s), which is
why clinic/nlu/llm_slots.py defaults to CPU. Re-run after a reboot / when
plugged in: if "GPU" wins, set CLINIC_LLM_NUM_GPU=auto. Read-only."""

import httpx

from clinic.nlu.llm_slots import MODEL, OLLAMA_URL


def run(label, num_gpu):
    options = {"temperature": 0, "num_predict": 40}
    if num_gpu is not None:
        options["num_gpu"] = num_gpu
    body = {"model": MODEL, "stream": False, "keep_alive": "30m", "options": options,
            "messages": [{"role": "user", "content": "Write the numbers one to thirty in English words separated by commas."}]}
    httpx.post(OLLAMA_URL, json=body, timeout=300)  # warm / (re)load for these settings
    r = httpx.post(OLLAMA_URL, json=body, timeout=300).json()
    print("%-22s %5.1f tokens/s" % (label, r["eval_count"] / (r["eval_duration"] / 1e9)))


if __name__ == "__main__":
    run("CPU (num_gpu=0)", 0)
    run("GPU / Ollama default", None)
