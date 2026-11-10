"""LoRA / QLoRA supervised fine-tuning on the files of ``prepare.py`` (TRL SFTTrainer, loss on
the completion only).

    CUDA_VISIBLE_DEVICES=0 python sft.py --config configs/repair.yaml
    # both cards, data parallel:
    accelerate launch --num_processes 2 sft.py --config configs/repair.yaml

The config is YAML (see configs/); any key can be overridden on the command line as
--set key=value (e.g. --set learning_rate=5e-5).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
import yaml
from peft import LoraConfig, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

from datasets import load_dataset


def _value(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args()
    cfg = yaml.safe_load(a.config.read_text(encoding="utf-8"))
    for item in a.set:
        key, _, value = item.partition("=")
        cfg[key] = _value(value)

    data = Path(cfg["data_dir"]).expanduser()
    out = Path(cfg["output_dir"]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1))

    tok = AutoTokenizer.from_pretrained(cfg["model"])
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    kwargs = {"torch_dtype": torch.bfloat16, "attn_implementation": cfg.get("attn", "sdpa")}
    if cfg.get("load_in_4bit"):
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    if torch.cuda.is_available():
        # one full copy per process (data parallel); no model sharding
        import os

        kwargs["device_map"] = {"": int(os.environ.get("LOCAL_RANK", 0))}
    model = AutoModelForCausalLM.from_pretrained(cfg["model"], **kwargs)
    if cfg.get("load_in_4bit"):
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    lora = LoraConfig(
        r=cfg.get("lora_r", 32),
        lora_alpha=cfg.get("lora_alpha", 64),
        lora_dropout=cfg.get("lora_dropout", 0.05),
        target_modules=cfg.get("target_modules", "all-linear"),
        task_type="CAUSAL_LM",
    )
    ds = load_dataset(
        "json",
        data_files={"train": str(data / "train.jsonl"), "validation": str(data / "val.jsonl")},
    )
    ds = ds.remove_columns(
        [c for c in ds["train"].column_names if c not in ("prompt", "completion")]
    )

    args = SFTConfig(
        output_dir=str(out),
        max_length=cfg.get("max_length", 8192),
        completion_only_loss=True,
        packing=False,
        per_device_train_batch_size=cfg.get("batch_size", 1),
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=cfg.get("grad_accum", 32),
        learning_rate=float(cfg.get("learning_rate", 1e-4)),
        lr_scheduler_type=cfg.get("scheduler", "cosine"),
        warmup_ratio=cfg.get("warmup_ratio", 0.03),
        num_train_epochs=cfg.get("epochs", 2),
        weight_decay=0.0,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        use_liger_kernel=cfg.get("liger", True),
        logging_steps=5,
        eval_strategy="steps",
        eval_steps=cfg.get("eval_steps", 50),
        save_strategy="steps",
        save_steps=cfg.get("eval_steps", 50),
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        report_to=cfg.get("report_to", "tensorboard"),
        seed=cfg.get("seed", 20261110),
        dataloader_num_workers=2,
        ddp_find_unused_parameters=False,
    )
    trainer = SFTTrainer(
        model=model,
        args=args,
        train_dataset=ds["train"],
        eval_dataset=ds["validation"],
        processing_class=tok,
        peft_config=lora,
    )
    trainer.train()
    trainer.save_model(str(out / "final"))
    tok.save_pretrained(str(out / "final"))
    metrics = trainer.evaluate()
    (out / "final" / "eval.json").write_text(json.dumps(metrics, indent=1))
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main()
