# Gobguessr

A small FastAPI game where players identify randomly selected images and compete for the longest correct-answer streak.

## Run it

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env
uv run uvicorn main:app --reload
```

Then open <http://127.0.0.1:8000>.

Place images in `images/`. They must be WebP files; the filename without `.webp` is the answer. For example, `red_apple.webp` accepts `red apple`, `red-apple`, or `RED APPLE`. The intended source dimensions are 64 × 44 pixels. Images are enlarged with nearest-neighbor rendering in the interface.

The SQLite database is created automatically as `gobguessr.db`. Set `GOBGUESSR_IMAGE_DIR` or `GOBGUESSR_DATABASE` to use different locations. Set a stable, private `GOBGUESSR_SECRET` in production so login sessions remain valid across restarts.

Leaderboard scores are daily and roll over at 00:00 UTC. Players with no correct guesses during the current UTC day are omitted.

Login is intentionally name-only, as requested: entering an existing name resumes that player's scores. It is suitable for a friendly/local game, not identity-sensitive use.
