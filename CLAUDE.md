# p2ptd-inventory

P2P Tech equipment inventory in InvenTree (https://p2ptd-inventory.online): import scripts, a Telegram bot for adding gear from photos (`bot/`), a nightly manual/photo scraper (`scraper/`), and an Android app build. Docs are in Russian in `docs/`. Write new docs, comments and bot messages in Russian too.

## Run and test

- Venv: `.venv` in the repo root (`uv venv && VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt`). It isn't portable; recreate it if the repo moves.
- Tests: `.venv/bin/python -m pytest -q`. CI runs the same tests (`.github/workflows/tests.yml`).
- Bot service: `p2ptd-bot` (systemd --user, unit in `systemd/`, installed copy in `~/.config/systemd/user/`). Keep both in sync. Restart with `systemctl --user restart p2ptd-bot` and check `journalctl --user -u p2ptd-bot -e`.
- Every import script has `--dry-run`. Use it first: these scripts write to the **live** InvenTree.

## Rules

- `.env` holds the InvenTree token and Telegram bot token. Never commit it, print it, or paste it anywhere. New settings go into `.env.example` with a placeholder value.
- `bot_data/` is runtime state (users, logs). Not in git.
- The bot can use the local model through olegovic-llm (`bot/olegovic.py` reads `~/.config/olegovic/config.toml`).
- Don't make old copies of the repo (`v3/`, `*-main.zip`). Use branches.
- `docs/SESSION_SUMMARY.md` and `docs/PLAN.md` track the current state. Update them after significant work.
