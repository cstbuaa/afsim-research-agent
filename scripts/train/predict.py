"""Greedy predictions with vLLM (base + LoRA, or the base alone as the baseline) on a
prepared split; the output goes to the Windows computer for scoring with the checker
(``python -m afsim_research_agent.training.score``).

    CUDA_VISIBLE_DEVICES=1 python predict.py --data ~/afsim-train/sft/repair/test.jsonl \
        --model Qwen/Qwen3-8B --lora ~/afsim-train/runs/repair-qwen3-8b-v1/final \
        --max-model-len 8192 --out preds_repair_test.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", default="Qwen/Qwen3-8B")
    ap.add_argument("--lora", type=Path, default=None, help="omit for the base-model baseline")
    ap.add_argument("--max-model-len", type=int, default=8192)
    ap.add_argument("--max-tokens", type=int, default=3072)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = [json.loads(x) for x in a.data.read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.limit:
        rows = rows[: a.limit]
    llm = LLM(
        model=a.model,
        enable_lora=a.lora is not None,
        max_lora_rank=64,
        max_model_len=a.max_model_len,
        gpu_memory_utilization=a.gpu_memory_utilization,
        enable_prefix_caching=True,
    )
    lora = LoRARequest("adapter", 1, str(a.lora.expanduser())) if a.lora else None
    params = SamplingParams(temperature=0.0, max_tokens=a.max_tokens)
    # the prompts are already in the chat format (prepare.py): no template here
    outs = llm.generate([r["prompt"] for r in rows], params, lora_request=lora)
    with a.out.open("w", encoding="utf-8") as f:
        for r, o in zip(rows, outs):
            f.write(
                json.dumps({"id": r["id"], "output": o.outputs[0].text}, ensure_ascii=False) + "\n"
            )
    print(f"{len(rows)} predictions -> {a.out}")


if __name__ == "__main__":
    main()
