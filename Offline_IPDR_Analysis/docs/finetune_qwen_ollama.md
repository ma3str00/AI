# Fine-tune Qwen for IPDR → load in Ollama (GGUF)

You run **Qwen via Ollama from a GGUF**. Ollama does **not** train.  
Flow: **export pairs → QLoRA on Hugging Face weights (Kaggle GPU) → merge → GGUF → `ollama create` → update `engines.yaml`**.

Target engine today: `qwen2.5-coder-14b:latest` (Kaggle, `engines.yaml`).

---

## 0. Prerequisites

| Need | Notes |
|------|--------|
| GPU | Kaggle notebook with GPU. **T4 16 GB** is tight for 14B; Unsloth QLoRA usually works. If OOM, fine-tune **7B** instead and still serve it from Ollama. |
| Data | `data/training_examples.jsonl` + `data/learned_examples.jsonl` (Mark correct). Prefer ≥300 clean SQL pairs. |
| Disk | Room for HF weights + GGUF (~8–20 GB depending on size/quant). |

---

## 1. Export training JSONL (on the app VM or laptop)

From `Offline_IPDR_Analysis`:

```bash
python scripts/export_finetune_jsonl.py \
  --out data/finetune_ipdr.jsonl \
  --val-out data/finetune_ipdr_val.jsonl
```

Each line looks like:

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "Question:\n...\n\nLabel hints:\n(none)\n\nSimilar questions:\n(none)"},
    {"role": "assistant", "content": "{\"sql\":\"SELECT ...\"}"}
  ]
}
```

Upload `finetune_ipdr.jsonl` (and optional val file) to the Kaggle notebook as a dataset or via “Add data” / upload.

Curate before training: drop wrong SQL, duplicates, and clarify-only noise unless you intentionally want clarify behavior.

---

## 2. Kaggle notebook — QLoRA with Unsloth

Prefer the ready notebook (your deploy flow + fine-tune):

- `docs/qwen-2-5-14b-ipdr-finetune-deploy.ipynb`
- or `Downloads/qwen-2-5-14b-ipdr-finetune-deploy.ipynb`

Set `RUN_FINETUNE`, `DATA_PATH`, and `MODEL_TAG` in the config cell, then run top to bottom.

Or create a GPU notebook and paste these cells in order.

### Cell A — install

```python
import os
os.environ["WANDB_DISABLED"] = "true"
# Do not pin old xformers — it fails to build on current Kaggle.
# Do NOT `pip install -U datasets trl` after unsloth — that pulls datasets 5.x / trl 1.19
# which Unsloth rejects. Internet ON. Fresh kernel. Do not import torch first.
!pip install -U pip
!pip install -U unsloth
!pip install --force-reinstall "datasets>=3.4.1,<5.0.0" "trl>=0.18.2,<=1.15.0,!=0.19.0"
```

If that fails, fall back to Unsloth’s Kaggle extra (still without upgrading datasets/trl past those pins):

```python
!pip install -U "unsloth[kaggle-new] @ git+https://github.com/unslothai/unsloth.git"
!pip install --force-reinstall "datasets>=3.4.1,<5.0.0" "trl>=0.18.2,<=1.15.0,!=0.19.0"
```


### Cell B — load 14B in 4-bit + LoRA

```python
from unsloth import FastLanguageModel
import torch

max_seq_length = 2048
model_name = "unsloth/Qwen2.5-Coder-14B-Instruct-bnb-4bit"  # or Qwen/Qwen2.5-Coder-14B-Instruct

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=model_name,
    max_seq_length=max_seq_length,
    dtype=None,
    load_in_4bit=True,
)

model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
    lora_alpha=16,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=42,
)
```

If this OOMs on T4, switch `model_name` to  
`unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit`.

### Cell C — load your JSONL

```python
from datasets import load_dataset

# Path after uploading the file into the notebook working dir / dataset
data_path = "/kaggle/input/your-dataset/finetune_ipdr.jsonl"  # adjust

ds = load_dataset("json", data_files={"train": data_path}, split="train")

def to_text(example):
    messages = example["messages"]
    # Chat template → single training string
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=False,
    )
    return {"text": text}

train_ds = ds.map(to_text, remove_columns=ds.column_names)
```

### Cell D — train

```python
from trl import SFTTrainer
from transformers import TrainingArguments

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=train_ds,
    dataset_text_field="text",
    max_seq_length=max_seq_length,
    packing=False,
    args=TrainingArguments(
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        warmup_steps=20,
        num_train_epochs=2,
        learning_rate=2e-4,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=10,
        output_dir="outputs_ipdr_lora",
        optim="adamw_8bit",
        seed=42,
        report_to="none",
    ),
)
trainer.train()
```

Tune lightly: 1–3 epochs. If loss collapses and answers become gibberish, lower LR or epochs.

### Cell E — quick sanity check

```python
FastLanguageModel.for_inference(model)
messages = [
    {"role": "system", "content": 'Write one SELECT for ipdr.ntc_ssl_log. Return only {"sql":"..."}.'},
    {"role": "user", "content": "Question:\nTop 10 app labels by download bytes yesterday\n\nLabel hints:\n(none)\n\nSimilar questions:\n(none)"},
]
prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
out = model.generate(**inputs, max_new_tokens=256, temperature=0.1)
print(tokenizer.decode(out[0], skip_special_tokens=True))
```

### Cell F — merge LoRA into 16-bit weights and save

```python
# Merged HF folder (needed before GGUF convert)
merged_dir = "/kaggle/working/qwen-ipdr-merged"
model.save_pretrained_merged(merged_dir, tokenizer, save_method="merged_16bit")
```

Download `qwen-ipdr-merged` (or push to a private HF repo).  
Also keep `outputs_ipdr_lora` if you want adapters only.

Optional Unsloth GGUF export (if it works in your notebook env):

```python
model.save_pretrained_gguf(
    "/kaggle/working/qwen-ipdr-gguf",
    tokenizer,
    quantization_method="q4_k_m",
)
```

If that fails, use llama.cpp convert on a machine with the merged HF folder (next section).

---

## 3. Convert merged HF → GGUF (llama.cpp)

On a machine with the merged folder and [llama.cpp](https://github.com/ggerganov/llama.cpp):

```bash
# Convert HF → f16 GGUF
python convert_hf_to_gguf.py /path/to/qwen-ipdr-merged \
  --outfile qwen2.5-coder-14b-ipdr-f16.gguf

# Quantize for Ollama / llama-server
./llama-quantize qwen2.5-coder-14b-ipdr-f16.gguf \
  qwen2.5-coder-14b-ipdr-q4_k_m.gguf Q4_K_M
```

Copy the `Q4_K_M` file to the host that runs Ollama (your Kaggle runtime or wherever the tunnel points).

---

## 4. Register the GGUF in Ollama

On the Ollama host, next to the GGUF:

```bash
cp deploy/Modelfile.qwen-ipdr.example Modelfile
# Edit FROM line if the filename differs
ollama create qwen2.5-coder-14b-ipdr -f Modelfile
ollama run qwen2.5-coder-14b-ipdr 'Return JSON only. {"sql":"SELECT 1"}'
```

Keep the old tag until you verify the new one.

---

## 5. Point the IPDR app at the new model

Edit `engines.yaml`:

```yaml
- id: kaggle
  label: Kaggle 14B IPDR
  kind: ollama
  base_url: "https://YOUR-TUNNEL.trycloudflare.com"
  model: "qwen2.5-coder-14b-ipdr"
  num_ctx: 8192
  keep_alive: -1
```

Restart / reload the assistant process if needed, update `kaggle.base_url` in `config.yaml`, then hard-refresh the UI and pick **Kaggle** as the engine.

Few-shot examples and Mark correct still apply on top of the fine-tuned weights — keep them.

---

## 6. If you also serve local llama.cpp from GGUF

Put the same `qwen2.5-coder-14b-ipdr-q4_k_m.gguf` on the VM, point `llama.model` in `config.yaml` at that filename, restart `llama-server` / `ipdr-assistant`. Local engine kind stays `llama.cpp`; no Ollama required on that path.

---

## Checklist

1. [ ] Export + curate `finetune_ipdr.jsonl`
2. [ ] QLoRA on Kaggle (14B or 7B if OOM)
3. [ ] Merge → GGUF Q4_K_M
4. [ ] `ollama create qwen2.5-coder-14b-ipdr`
5. [ ] Update `engines.yaml` `model:`
6. [ ] Smoke-test known questions (time filters, subscriber `_` exclude, app LIKE)

---

## What not to do

- Do not expect Ollama to fine-tune the live GGUF.
- Do not train on unreviewed Mark-correct rows that still have bad SQL.
- Do not drop the example bank after fine-tuning; retrieval still helps follow-ups.
