from __future__ import annotations

import os
import random
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = Path(os.getenv("GOBGUESSR_IMAGE_DIR", BASE_DIR / "images"))
DATABASE_PATH = Path(os.getenv("GOBGUESSR_DATABASE", BASE_DIR / "gobguessr.db"))
SESSION_SECRET = os.getenv("GOBGUESSR_SECRET", secrets.token_hex(32))

templates = Jinja2Templates(directory=BASE_DIR / "templates")
PROMPTS = (
    "what the fuck is this?",
    "what is this creature?",
    "what is this fucking thing?",
)


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialise_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                current_streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0,
                score_date TEXT NOT NULL DEFAULT '1970-01-01',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                image_name TEXT NOT NULL,
                answered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS rounds_player_id ON rounds(player_id);
            """
        )
        player_columns = {row["name"] for row in connection.execute("PRAGMA table_info(players)")}
        if "score_date" not in player_columns:
            connection.execute("ALTER TABLE players ADD COLUMN score_date TEXT NOT NULL DEFAULT '1970-01-01'")
        # Older versions could create a new unanswered round on every refresh.
        # Keep only the newest one before enforcing one active round per player.
        connection.execute(
            """
            DELETE FROM rounds
            WHERE answered = 0
              AND rowid NOT IN (
                  SELECT MAX(rowid) FROM rounds WHERE answered = 0 GROUP BY player_id
              )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_active_round_per_player ON rounds(player_id) WHERE answered = 0"
        )


def utc_date() -> str:
    return datetime.now(UTC).date().isoformat()


def reset_player_if_needed(connection: sqlite3.Connection, player_id: int) -> None:
    today = utc_date()
    connection.execute(
        """
        UPDATE players
        SET current_streak = 0, best_streak = 0, score_date = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND score_date != ?
        """,
        (today, player_id, today),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialise_database()
    yield


app = FastAPI(title="Gobguessr", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=os.getenv("GOBGUESSR_HTTPS", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def player_from_request(request: Request) -> sqlite3.Row | None:
    player_id = request.session.get("player_id")
    if not isinstance(player_id, int):
        return None
    with database() as connection:
        reset_player_if_needed(connection, player_id)
        return cast(
            "sqlite3.Row | None", connection.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        )


def image_names() -> list[str]:
    if not IMAGE_DIR.exists():
        return []
    return sorted(path.name for path in IMAGE_DIR.iterdir() if path.is_file() and path.suffix.casefold() == ".webp")


def normalise_answer(value: str) -> str:
    value = value.strip().casefold().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value)


def create_round(player_id: int) -> str | None:
    choices = image_names()
    if not choices:
        return None

    with database() as connection:
        # Serialize round creation so simultaneous refreshes cannot create two
        # different active pictures for the same player.
        connection.execute("BEGIN IMMEDIATE")
        active_round = connection.execute(
            """
            SELECT id, image_name
            FROM rounds
            WHERE player_id = ? AND answered = 0
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (player_id,),
        ).fetchone()
        if active_round and active_round["image_name"] in choices:
            return cast("str", active_round["id"])
        if active_round:
            connection.execute("DELETE FROM rounds WHERE id = ?", (active_round["id"],))

        previous = connection.execute(
            "SELECT image_name FROM rounds WHERE player_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        if previous and len(choices) > 1 and previous["image_name"] in choices:
            choices.remove(previous["image_name"])
        round_id = secrets.token_urlsafe(24)
        connection.execute(
            "INSERT INTO rounds (id, player_id, image_name) VALUES (?, ?, ?)",
            (round_id, player_id, random.choice(choices)),
        )
        connection.execute(
            "DELETE FROM rounds WHERE player_id = ? AND answered = 1 AND id != ?",
            (player_id, round_id),
        )
    return round_id


def leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    with database() as connection:
        return cast(
            "list[sqlite3.Row]",
            connection.execute(
                """
                SELECT name, current_streak, best_streak
                FROM players
                WHERE score_date = ? AND best_streak > 0
                ORDER BY best_streak DESC, current_streak DESC, updated_at ASC
                LIMIT ?
                """,
                (utc_date(), limit),
            ).fetchall(),
        )


@app.get("/landing-image", name="landing_image")
async def landing_image():
    choices = image_names()
    if not choices:
        raise HTTPException(status_code=404)
    image_path = IMAGE_DIR / random.choice(choices)
    return Response(image_path.read_bytes(), media_type="image/webp", headers={"Cache-Control": "no-store"})


@app.get("/")
async def home(request: Request):
    if player_from_request(request) is None:
        return templates.TemplateResponse(request, "login.html", {"error": None, "has_images": bool(image_names())})
    return RedirectResponse("/play", status_code=303)


@app.post("/login")
async def login(request: Request, name: Annotated[str, Form()]):
    clean_name = " ".join(name.split())
    if not clean_name or len(clean_name) > 30:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Choose a name between 1 and 30 characters.", "has_images": bool(image_names())},
            status_code=422,
        )

    with database() as connection:
        player = connection.execute("SELECT id FROM players WHERE name_key = ?", (clean_name.casefold(),)).fetchone()
        if player is None:
            cursor = connection.execute(
                "INSERT INTO players (name, name_key, score_date) VALUES (?, ?, ?)",
                (clean_name, clean_name.casefold(), utc_date()),
            )
            player_id = cursor.lastrowid
        else:
            player_id = player["id"]
            reset_player_if_needed(connection, player_id)
    request.session.clear()
    request.session["player_id"] = player_id
    return RedirectResponse("/play", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/play")
async def play(request: Request):
    player = player_from_request(request)
    if player is None:
        return RedirectResponse("/", status_code=303)

    round_id = create_round(player["id"])
    feedback = request.session.pop("feedback", None)
    return templates.TemplateResponse(
        request,
        "play.html",
        {
            "player": player,
            "round_id": round_id,
            "leaders": leaderboard(),
            "prompt": random.choice(PROMPTS),
            "feedback": feedback,
        },
    )


@app.get("/rounds/{round_id}/image", name="round_image")
async def round_image(request: Request, round_id: str):
    player = player_from_request(request)
    if player is None:
        raise HTTPException(status_code=401)
    with database() as connection:
        reset_player_if_needed(connection, player["id"])
        game_round = connection.execute(
            "SELECT image_name FROM rounds WHERE id = ? AND player_id = ? AND answered = 0",
            (round_id, player["id"]),
        ).fetchone()
    if game_round is None:
        raise HTTPException(status_code=404)
    image_path = IMAGE_DIR / game_round["image_name"]
    if not image_path.is_file() or image_path.suffix.casefold() != ".webp":
        raise HTTPException(status_code=404)
    return Response(image_path.read_bytes(), media_type="image/webp", headers={"Cache-Control": "no-store"})


@app.post("/guess")
async def guess(request: Request, round_id: Annotated[str, Form()], answer: Annotated[str, Form()]):
    player = player_from_request(request)
    if player is None:
        return RedirectResponse("/", status_code=303)

    with database() as connection:
        game_round = connection.execute(
            "SELECT image_name FROM rounds WHERE id = ? AND player_id = ? AND answered = 0",
            (round_id, player["id"]),
        ).fetchone()
        if game_round is None:
            return RedirectResponse("/play", status_code=303)

        correct_answer = Path(game_round["image_name"]).stem
        is_correct = normalise_answer(answer) == normalise_answer(correct_answer)
        connection.execute("UPDATE rounds SET answered = 1 WHERE id = ?", (round_id,))
        if is_correct:
            connection.execute(
                """
                UPDATE players
                SET current_streak = current_streak + 1,
                    best_streak = MAX(best_streak, current_streak + 1),
                    score_date = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (utc_date(), player["id"]),
            )
        else:
            connection.execute(
                "UPDATE players SET current_streak = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (player["id"],),
            )
    request.session["feedback"] = "correct" if is_correct else "wrong"
    return RedirectResponse("/play", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
