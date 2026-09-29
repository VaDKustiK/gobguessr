from pathlib import Path

import httpx
import pytest

import main


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_client(tmp_path: Path, monkeypatch) -> httpx.AsyncClient:
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "red_apple.webp").write_bytes(b"RIFFfake-webp")
    (image_dir / "pear.webp").write_bytes(b"RIFFfake-webp")
    monkeypatch.setattr(main, "IMAGE_DIR", image_dir)
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "test.db")
    main.initialise_database()
    transport = httpx.ASGITransport(app=main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_login_and_correct_guess(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as client:
        response = await client.post("/login", data={"name": "Ada"}, follow_redirects=True)
        assert response.status_code == 200
        assert "Playing as <strong>Ada</strong>" in response.text

        round_id = response.text.split('name="round_id" value="', 1)[1].split('"', 1)[0]
        with main.database() as connection:
            image = connection.execute("SELECT image_name FROM rounds WHERE id = ?", (round_id,)).fetchone()[0]

        assert image not in response.text
        image_response = await client.get(f"/rounds/{round_id}/image")
        assert image_response.status_code == 200
        assert image_response.headers["content-type"] == "image/webp"

        answer = Path(image).stem.replace("_", "-").upper()
        response = await client.post("/guess", data={"round_id": round_id, "answer": answer}, follow_redirects=True)
        assert response.status_code == 200
        assert "Nice eye!" not in response.text
        assert 'class="score score-bump">1</strong>' in response.text


@pytest.mark.anyio
async def test_refresh_keeps_the_same_active_picture(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as client:
        first_page = await client.post("/login", data={"name": "Refreshproof"}, follow_redirects=True)
        first_round = first_page.text.split('name="round_id" value="', 1)[1].split('"', 1)[0]

        refreshed_page = await client.get("/play")
        refreshed_round = refreshed_page.text.split('name="round_id" value="', 1)[1].split('"', 1)[0]

        assert refreshed_round == first_round
        with main.database() as connection:
            active_rounds = connection.execute("SELECT COUNT(*) FROM rounds WHERE answered = 0").fetchone()[0]
        assert active_rounds == 1


@pytest.mark.anyio
async def test_wrong_answer_resets_current_but_keeps_best(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as client:
        response = await client.post("/login", data={"name": "Grace"}, follow_redirects=True)
        round_id = response.text.split('name="round_id" value="', 1)[1].split('"', 1)[0]
        with main.database() as connection:
            connection.execute(
                "UPDATE players SET current_streak = 3, best_streak = 5, score_date = ? WHERE name_key = 'grace'",
                (main.utc_date(),),
            )

        response = await client.post(
            "/guess", data={"round_id": round_id, "answer": "definitely wrong"}, follow_redirects=True
        )
        assert "YOU ARE A DUMBASS" in response.text
        with main.database() as connection:
            player = connection.execute("SELECT current_streak, best_streak FROM players").fetchone()
        assert tuple(player) == (0, 5)


@pytest.mark.anyio
async def test_leaderboard_ignores_zero_scores_and_resets_at_utc_midnight(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as client:
        response = await client.post("/login", data={"name": "Lin"}, follow_redirects=True)
        assert "No scores yet today." in response.text
        assert '<span class="leader-name">Lin' not in response.text

        with main.database() as connection:
            connection.execute(
                """
                UPDATE players
                SET current_streak = 4, best_streak = 8, score_date = '2000-01-01'
                WHERE name_key = 'lin'
                """
            )

        response = await client.get("/play")
        assert 'class="score ">0</strong>' in response.text
        assert '<span class="leader-name">Lin' not in response.text


@pytest.mark.anyio
async def test_name_validation_and_protected_image(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as client:
        landing = await client.get("/")
        assert "/landing-image" in landing.text
        assert "No password needed" not in landing.text
        assert (await client.get("/landing-image")).headers["content-type"] == "image/webp"

        response = await client.post("/login", data={"name": "x" * 31})
        assert response.status_code == 422
        assert "between 1 and 30" in response.text
        assert (await client.get("/rounds/not-a-round/image")).status_code == 401
