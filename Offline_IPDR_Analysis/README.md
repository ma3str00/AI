# IPDR assistant

Offline question answering for `ipdr.ntc_ssl_logs`. The llama.cpp server already running on this VM writes one read-only `SELECT`, MySQL runs it, and the same server explains the rows.

## Time

`start_time`, `end_time`, and `recv_time` are Unix epoch seconds. The sample value `1790604000` is 2026-09-28 19:00:00 GMT+5. The MySQL session uses `+05:00`. If every answer is five hours off, set `time.stored_as` to `local_epoch` and restart the web app.

## Mobile and fixed-line

A numeric `subscriber_id` can be either. A row is mobile when `mcc`, `mnc`, `lac`, `cid`, or `usr_loc_info` is filled. A row is fixed-line when those cell fields are empty and `geo` or `loc` is filled. A `subscriber_id` containing `_` is an unidentified client IP and port.

## Run on the Ubuntu VM

llama-server stays on port 11434. This page uses port 8090.

1. Clone the repo and enter `Offline_IPDR_Analysis`.
2. Create the read-only user in `scripts/readonly-user.sql`.
3. In a maintenance window, run `scripts/recommended-indexes.sql`. `start_time` is not indexed yet.
4. Run `bash install-ubuntu.sh`.
5. Set the MySQL password in `config.yaml`. Change `mysql.host` when MySQL moves. Restart with `sudo systemctl restart ipdr-assistant`.

Open `http://127.0.0.1:8090`.

## llama-server

The running unit already has the right batch settings: context 8192, one slot, `-b 2048`, `-ub 512`, `-fa auto`, port 11434. A logged request processed the prompt at about 13 tokens/s and generated at about 9 tokens/s. The web app sends four similar examples per question so the prompt stays inside that 8192 context.

`deploy/llama-server.service.recommended` keeps that unit and changes three things: 16 threads for generation, 28 threads for the prompt (`-tb 28`), `--mlock`, and `--host 127.0.0.1`. Generation stays near 9 tokens/s on this 3B model. The larger gain is SQL accuracy: with 62 GB of RAM, `Qwen2.5-Coder-7B-Instruct` Q4_K_M is the next model file to put on `-m`. Point `llama.model` at the new file name after the swap.

## Examples

`data/training_examples.jsonl` holds the question bank. Regenerate it with:

```bash
python scripts/generate_examples.py
python scripts/test_guard.py
```
