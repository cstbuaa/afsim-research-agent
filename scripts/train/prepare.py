"""Turn the pilot bundle into SFT files for one task: prompt / completion text in the Qwen3
chat format with thinking off (the way vLLM serves it to Falcon), token lengths checked.

    python prepare.py --bundle ~/afsim-train/data/pilot_fp_v1 --task repair \
        --model Qwen/Qwen3-8B --max-length 8192 --out ~/afsim-train/sft/repair

Writes train.jsonl / val.jsonl / test.jsonl ({"id", "prompt", "completion"}) and stats.json.
Samples longer than --max-length are dropped and counted (the prompt is never cut: a cut
prompt teaches the model to answer without the part it lost).
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from transformers import AutoTokenizer

FILES = {
    "repair": "repair/repair.jsonl",
    "structure": "sft/structure.jsonl",
    "control": "sft/control.jsonl",
    "verification": "sft/verification.jsonl",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--task", choices=[*FILES, "all"], required=True)
    ap.add_argument("--model", default="Qwen/Qwen3-8B")
    ap.add_argument("--max-length", type=int, default=8192)
    ap.add_argument(
        "--list-keep",
        type=float,
        default=0.5,
        help="share of list-style requirement samples kept (they are close to the facts)",
    )
    ap.add_argument("--tag", action="store_true", help="prefix the task name (multi-task)")
    ap.add_argument("--seed", type=int, default=20261110)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(a.model)
    eos = tok.eos_token or "<|im_end|>"
    rng = random.Random(a.seed)
    tasks = list(FILES) if a.task == "all" else [a.task]
    out: dict[str, list[dict[str, str]]] = {"train": [], "val": [], "test": []}
    stats: Counter[str] = Counter()
    lengths: dict[str, list[int]] = {"prompt": [], "completion": []}
    for task in tasks:
        for line in (a.bundle / FILES[task]).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("style") == "list" and r["split"] == "train" and rng.random() > a.list_keep:
                stats[f"{task}:list_dropped"] += 1
                continue
            messages = r["messages"]
            if a.tag:
                messages = [
                    {"role": "user", "content": f"[任务：{task}]\n" + messages[0]["content"]}
                ]
            prompt = tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
            completion = r["target"] + eos
            n_p = len(tok(prompt, add_special_tokens=False)["input_ids"])
            n_c = len(tok(completion, add_special_tokens=False)["input_ids"])
            if n_p + n_c > a.max_length:
                stats[f"{task}:too_long"] += 1
                continue
            lengths["prompt"].append(n_p)
            lengths["completion"].append(n_c)
            out[r["split"]].append({"id": r["id"], "prompt": prompt, "completion": completion})
            stats[f"{task}:{r['split']}"] += 1
    a.out.mkdir(parents=True, exist_ok=True)
    for split, rows in out.items():
        rng.shuffle(rows)
        with (a.out / f"{split}.jsonl").open("w", encoding="utf-8") as f:
            for x in rows:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")

    def pct(xs: list[int], q: float) -> int:
        xs = sorted(xs)
        return xs[int(q * (len(xs) - 1))] if xs else 0

    info = {
        "model": a.model,
        "task": a.task,
        "max_length": a.max_length,
        "counts": dict(stats),
        "tokens": {
            k: {"p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": max(v, default=0)}
            for k, v in lengths.items()
        },
    }
    (a.out / "stats.json").write_text(json.dumps(info, ensure_ascii=False, indent=1))
    print(json.dumps(info, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
