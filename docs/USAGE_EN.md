# NovelAgent User Guide

[中文](USAGE.md) · [Back to README](../README_EN.md)

## 1. Requirements

- Python 3.10+
- A working DeepSeek-compatible API account
- Optional: xAI API for non-canonical DLC expansion
- Optional: `llama-server` plus a GGUF embedding model for vector memory

Run the application in a private working directory. Do not make your live manuscript directory a public Git repository.

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python app.py
```

You may also run `启动NovelAgent_控制台.bat`. Use the hidden-window launcher only after the console launch works correctly.

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.example.json config.json
export DEEPSEEK_API_KEY='replace-me'
export XAI_API_KEY='replace-me'   # optional
python app.py
```

Do not put real exports in a script that will be committed. Saving an API key through the web UI is rejected on non-Windows systems to prevent weakly encrypted local persistence.

## 2. Initial configuration

If `config.json` is missing, the first run copies `config.example.json`. Local configuration is excluded by `.gitignore`.

Common fields:

| Field | Purpose |
| --- | --- |
| `web.host` | Defaults to `127.0.0.1`, local access only |
| `web.port` | Defaults to `7860` |
| `deepseek.source` | `official` or `volcengine_agent_plan` |
| `generation.chapters_per_run` | Chapters generated per run |
| `generation.target_chapter_chars` | Reference chapter length |
| `embedding.enabled` | Enables embedding retrieval |
| `embedding_server.auto_start` | Lets NovelAgent start the local embedding process |
| `context.outline_range_overrides` | Splits very large outlines by chapter range |
| `external_canon.ranges` | Reserves canonical chapter ranges for external production |

Model identifiers must be supported by the selected account and endpoint. If a default is unavailable, edit the corresponding `model` field in `config.json`.

## 3. Prepare story sources

Maintain at least these files before generation:

| File | Recommended contents |
| --- | --- |
| `story/premise.md` | Core premise, genre, main conflict, end direction |
| `story/world.md` | Setting, era, locations, power or technology rules |
| `story/characters_seed.md` | Identity, personality, goals, relationships, knowledge boundaries |
| `story/outline.md` | Chapter-level events and outcomes |
| `story/style.md` | Viewpoint, language, pacing, prohibited patterns, chapter structure |
| `story/author_notes.md` | Author notes outside the formal canon priority order |

Do not place API keys, real addresses, identity numbers, private messages, or material that cannot be sent to a third-party model provider in these files. Relevant context leaves the machine when a cloud model is called.

## 4. Authentication and APIs

1. Open `http://127.0.0.1:7860`.
2. Create an administrator username and a password of at least eight characters.
3. Choose the official DeepSeek or Volcengine Agent Plan source.
4. On Windows, save the key in the dashboard; it is written to `runtime/*.dpapi`.
5. Configure an xAI key only if DLC expansion is needed.
6. Test each connection before starting generation.

After changing Windows users, reinstalling the OS, or moving to another machine, expect to enter the keys again. DPAPI files are not a portable password vault.

## 5. Vector memory

Vector memory is not required to start the web app, but it improves long-form retrieval.

1. Prepare an embedding-capable `llama-server`.
2. Prepare a GGUF embedding model.
3. Edit `config.json`:

```json
{
  "embedding_server": {
    "auto_start": true,
    "llama_server_path": "C:\\path\\to\\llama-server.exe",
    "model_path": "C:\\path\\to\\embedding-model.gguf",
    "host": "127.0.0.1",
    "port": 8081
  }
}
```

Do not replace the entire configuration with this fragment. Alternatively, keep auto-start disabled and provide a compatible OpenAI embeddings endpoint plus `/health` yourself.

## 6. Generate Canon

1. Verify story sources and the next chapter in `state.json`.
2. Select a cost profile, batch size, target length, and revision limit.
3. Start Canon.
4. Observe Plan, Draft, Review, Revision, and Summary/Memory stages.
5. If context or historical-cost protection pauses the run, inspect the notice and explicitly continue or cancel.

After the final quality gate passes, the system writes:

- `chapters/NNNN.md`
- `plans/NNNN.md`
- `reviews/NNNN.json`
- `summaries/NNNN.md`
- `handoffs/NNNN.json`
- `runtime/state_snapshots/NNNN.json`
- `novel_memory.sqlite3`

Do not edit these files while a run is active.

## 7. Audit, repair, and rollback

- Story audit uses overlapping windows and writes reports under `reports/`.
- Audit-driven repair creates candidates before changing Canon.
- Inspect candidate diffs, local validation, and joint review before an explicit commit.
- Repair commits and rewrite-from operations create archives under `archive/`.
- Hashes detect manual changes made after a commit so rollback does not silently overwrite them.

Always sample-check motivation, voice, implicit relationships, and style. These are not fully captured by deterministic rules.

## 8. DLC, reader reflow, and export

- DLC processes `<DLC_SCENE .../>` markers and writes under `dlc/`; it does not automatically change Canon.
- `prompts/expansion_reference.md` is a public generic reference and can be replaced with your own safe material.
- Reader reflow changes paragraph boundaries only, verifies exact text preservation, and writes to `reader_chapters/`.
- Export supports Markdown, text, and ZIP formats.

## 9. Troubleshooting

### The page does not open

Confirm that Uvicorn is listening on `127.0.0.1:7860` and that another process is not using port 7860.

### API shows “not configured”

On Windows, save the key again. On Linux/macOS, ensure the variable is exported in the same shell that starts `python app.py`.

### Embedding stays stopped

`auto_start` is false by default. If enabled, verify the executable, model path, port 8081, and `logs/embed_stderr.log`.

### Can the app run without embeddings?

Yes. SQLite and text retrieval still work, but semantic recall of long-term memories is weaker.

### Publishing your fork

Run:

```powershell
python -m pytest -q
git status --short
git ls-files
```

Confirm that `config.json`, `runtime/`, databases, logs, manuscript text, private story bibles, and backup archives are absent from `git ls-files`. Rotate any credential that was ever committed; deleting it from the latest revision does not remove it from Git history.
