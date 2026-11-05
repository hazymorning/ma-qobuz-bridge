"""What this repo changes inside upstream QobuzProxy files: the hooks in app.py, speaker.py,
playback/player.py, backends/base.py, backends/types.py and webui/routes.py, and the fixes in
auth/api_client.py and connect/ws_manager.py.

Run these after merging upstream: each one fails if the merge lost one of those changes.
"""

import asyncio
import contextlib
import functools
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from qobuz_proxy import app as app_module
from qobuz_proxy import speaker as speaker_module
from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.auth.api_client import QobuzAPIClient
from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.music_assistant import connection as connection_module
from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant import speakers as speakers_module
from qobuz_proxy.config import Config, QobuzConfig, ServerConfig, SpeakerConfig
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QobuzQueue
from qobuz_proxy.webui.config_writer import save_config
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.fake_qobuz import (
    APP_PLAYING,
    APP_STOPPED,
    StubDiscovery,
    StubMetadata,
    album_state,
    make_tokens,
    set_active,
    set_max_quality,
    set_volume,
    volume_broadcast,
)
from tests.rig import KITCHEN, wait_for

CD_QUALITY = 2  # what the app sends for CD quality; Qobuz calls it format 6


async def test_a_volume_broadcast_reaches_only_the_active_speaker_but_a_direct_command_always_does(
    rig,
):
    await rig.deliver(volume_broadcast(25))
    await rig.wait_for(lambda: rig.volumes_set() == [25], "volume 25")

    await rig.deliver(set_active(False))  # the app now plays on another device
    await rig.deliver(volume_broadcast(60))
    await rig.deliver(set_volume(45))  # addressed to this speaker
    await rig.wait_for(lambda: rig.volumes_set()[-1] == 45, "volume 45")

    assert rig.volumes_set() == [25, 45]


async def test_a_volume_change_in_music_assistant_shows_in_the_app_only_while_the_speaker_is_active(
    rig,
):
    rig.server.set_player_volume(KITCHEN, 40)
    await rig.wait_for(lambda: 40 in rig.cloud.volumes, "the app shows volume 40")

    await rig.deliver(set_active(False))
    rig.server.set_player_volume(KITCHEN, 70)
    await rig.idle(0.2)

    assert 70 not in rig.cloud.volumes


async def test_leaving_a_speaker_in_the_app_tells_its_backend_once_the_music_has_stopped(rig):
    states_when_left = []
    rig.speaker._backend.app_left = lambda: states_when_left.append(rig.queue_state())
    await rig.play_album(0)

    await rig.deliver(set_active(False))  # the app now plays on another device
    await rig.wait_for(lambda: states_when_left, "the backend heard the app left")
    await rig.deliver(set_active(False), set_active(True))  # left and picked again at once

    assert states_when_left == [MusicAssistantState.IDLE]


async def test_playing_from_a_position_starts_music_assistant_there_without_a_seek(rig):
    await rig.deliver(album_state(0, APP_PLAYING, pos=61_000))
    await rig.wait_for(
        lambda: rig.queue_state() == MusicAssistantState.PLAYING, "Music Assistant playing"
    )

    assert rig.queue_position() == pytest.approx(61, abs=1)
    assert rig.server.calls("player_queues/seek") == []


async def test_a_track_music_assistant_cannot_play_is_skipped_when_play_is_pressed_on_it(rig):
    rig.server.unplayable_track_ids = {"102"}
    await rig.deliver(album_state(1, APP_STOPPED))  # the app has track 2 up, stopped

    await rig.deliver(album_state(1, APP_PLAYING))

    await rig.wait_for(
        lambda: rig.queue_track() == "103" and rig.app() == (APP_PLAYING, 3), "track 3"
    )
    assert rig.queue_state() == MusicAssistantState.PLAYING


async def test_music_assistant_streams_the_track_itself_so_the_add_on_never_restarts_or_reports_it(
    rig,
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    await rig.deliver(set_max_quality(CD_QUALITY))
    await rig.idle(0.2)

    assert rig.metadata.max_qualities == [6]  # the speaker took the app's choice...
    assert rig.writes() == []  # ...without restarting the track for it
    assert rig.app() == (APP_PLAYING, 1)
    # Music Assistant's Qobuz provider reports every play; a second report would count it twice.
    # The Qobuz API client the rig gave the speaker is the only place a report would show.
    assert not rig.speaker._api_client.report_streaming_start.called


class Renderer(AudioBackend):
    """A device whose state the test sets, counting how often the player polls it.

    Like DLNA and local speakers, it keeps AudioBackend's answer on resumes done elsewhere.
    """

    def __init__(self) -> None:
        super().__init__(name="renderer")
        self.state = PlaybackState.STOPPED
        self.polls = 0

    async def get_state(self) -> PlaybackState:
        self.polls += 1
        return self.state

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        self.state = PlaybackState.PLAYING

    async def pause(self) -> None:
        self.state = PlaybackState.PAUSED

    async def resume(self) -> bool:
        self.state = PlaybackState.PLAYING
        return True

    async def stop(self, *, next_track_id: str | None = None) -> None:
        self.state = PlaybackState.STOPPED

    async def seek(self, position_ms: int) -> None: ...

    async def get_position(self) -> int:
        return 0

    async def set_volume(self, level: int) -> None: ...

    async def get_volume(self) -> int:
        return 50

    async def connect(self) -> bool:
        self._is_connected = True
        return True

    async def disconnect(self) -> None: ...


class MusicAssistantRenderer(Renderer):
    """A device another controller can resume, as Music Assistant's own UI can."""

    @property
    def reports_external_resume(self) -> bool:
        return True


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sleep lasts over 1 ms, so the player polls its device that often, not every 0.5 s."""
    sleep = asyncio.sleep

    async def short_sleep(delay: float, result: Any = None) -> Any:
        return await sleep(min(delay, 0.001), result)

    monkeypatch.setattr(asyncio, "sleep", short_sleep)


@contextlib.asynccontextmanager
async def _paused_player(renderer: Renderer) -> AsyncIterator[QobuzPlayer]:
    player = QobuzPlayer(queue=QobuzQueue(), metadata_service=StubMetadata(), backend=renderer)
    await player.start()
    try:
        await player.play_track(queue_item_id=1, track_id="101")
        await player.pause()
        yield player
    finally:
        await player.stop()


@pytest.mark.usefixtures("fast_polls")
async def test_a_paused_player_stops_with_its_device_but_resumes_only_if_its_backend_reports_it():
    music_assistant = MusicAssistantRenderer()
    dlna = Renderer()
    async with _paused_player(music_assistant) as followed, _paused_player(dlna) as ignored:
        music_assistant.state = PlaybackState.PLAYING  # resumed in Music Assistant's own UI
        dlna.state = PlaybackState.PLAYING  # a renderer that still reports a stale state
        await wait_for(lambda: followed.state == PlaybackState.PLAYING, "the resume followed")
        polls = dlna.polls
        await wait_for(lambda: dlna.polls >= polls + 5, "five more polls of the other device")
        assert ignored.state == PlaybackState.PAUSED

        await followed.pause()
        music_assistant.state = PlaybackState.STOPPED  # e.g. its queue was cleared
        await wait_for(lambda: followed.state == PlaybackState.STOPPED, "the player stopped")


@contextlib.asynccontextmanager
async def _add_on(config: Config) -> AsyncIterator[tuple[QobuzProxy, aiohttp.ClientSession]]:
    """The whole add-on running with ``config``, and a client for its web UI."""
    proxy = QobuzProxy(config)
    await proxy.start()
    port = proxy._web_runner.addresses[0][1]  # the free port http_port=0 picked
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{port}") as web_ui:
            yield proxy, web_ui
    finally:
        await proxy.stop()


class _QobuzAcceptingTheSavedLogin:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.user_auth_token = ""

    async def login_with_token(self, user_id: str, auth_token: str) -> bool:
        self.user_auth_token = auth_token
        return True


async def _qobuz_sign_in_with_a_new_token(code: str, **_: object) -> dict[str, str]:
    return {"user_id": "1", "user_auth_token": "new"}


@pytest.fixture
def config_from_an_earlier_run(
    server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch
) -> Config:
    """Qobuz and Music Assistant were both set up in an earlier run; returns the add-on's config."""
    settings.save(
        app_module.CACHE_DIR / "music_assistant.json",
        settings.Settings(url=server.url, token=server.token),
    )
    monkeypatch.setattr(
        connection_module,
        "MusicAssistantConnection",
        functools.partial(
            connection_module.MusicAssistantConnection, client_factory=server.client_factory
        ),
    )
    monkeypatch.setattr(speakers_module, "UPDATE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("qobuz_proxy.app.QobuzAPIClient", _QobuzAcceptingTheSavedLogin)
    monkeypatch.setattr("qobuz_proxy.webui.routes.exchange_code", _qobuz_sign_in_with_a_new_token)
    monkeypatch.setattr(speaker_module, "DiscoveryService", StubDiscovery)
    monkeypatch.setattr(speaker_module, "MetadataService", lambda **kwargs: StubMetadata())
    return Config(
        qobuz=QobuzConfig(user_id="1", auth_token="saved"),
        server=ServerConfig(http_port=0, bind_address="127.0.0.1"),
    )


async def _speakers_in_the_qobuz_app(web_ui: aiohttp.ClientSession) -> int:
    response = await web_ui.get("/api/music-assistant/status")
    return (await response.json())["speaker_count"]


def _qobuz_logins_the_speakers_use(proxy: QobuzProxy) -> list[str]:
    speakers = proxy._music_assistant._speakers.values()
    return [speaker._api_client.user_auth_token for speaker in speakers]


async def test_music_assistant_players_are_in_the_qobuz_app_only_while_the_add_on_runs_logged_in(
    server, config_from_an_earlier_run
):
    async with _add_on(config_from_an_earlier_run) as (proxy, web_ui):
        await wait_for(lambda: _speakers_in_the_qobuz_app(web_ui), "the Kitchen speaker")
        assert await _speakers_in_the_qobuz_app(web_ui) == 1
        assert _qobuz_logins_the_speakers_use(proxy) == ["saved"]

        # Logging in again, e.g. to another account, moves the running speaker to the new login
        signed_in = await web_ui.get("/auth/callback?code_autorisation=1", allow_redirects=False)
        assert signed_in.headers["Location"] == "/"  # back to the web UI, no error
        assert _qobuz_logins_the_speakers_use(proxy) == ["new"]

        assert (await web_ui.post("/api/auth/logout", json={})).status == 200
        assert await _speakers_in_the_qobuz_app(web_ui) == 0

    await server.settle()
    assert server.clients == []  # the add-on closed its Music Assistant connection


@pytest.fixture
async def web_ui(tmp_path, monkeypatch) -> AsyncIterator[aiohttp.ClientSession]:
    """The add-on's web UI, logged in to Qobuz, with a DLNA speaker saved in config.yaml."""
    monkeypatch.setattr("qobuz_proxy.app.load_user_token", lambda: None)
    config = Config(
        server=ServerConfig(http_port=0, bind_address="127.0.0.1"),
        speakers=[SpeakerConfig(name="Living Room", dlna_ip="192.168.1.20")],
        config_path=tmp_path / "config.yaml",
    )
    save_config(config, config.config_path)
    async with _add_on(config) as (proxy, session):
        # Logged in after start, so the add-on never tries to reach the DLNA speaker
        proxy._api_client = MagicMock()
        yield session


async def test_a_speaker_the_next_start_would_reject_is_refused_in_the_web_ui_and_not_saved(
    web_ui, tmp_path
):
    saved = (tmp_path / "config.yaml").read_text()

    added = await web_ui.post("/api/speakers", json={"name": "Kitchen", "backend": "bogus"})
    edited = await web_ui.put("/api/speakers/living-room", json={"dlna_ip": ""})

    assert (added.status, edited.status) == (400, 400)
    assert "unknown backend type 'bogus'" in (await added.json())["error"]
    assert "DLNA IP address is required" in (await edited.json())["error"]
    assert (tmp_path / "config.yaml").read_text() == saved
    listed = await (await web_ui.get("/api/speakers")).json()
    assert [(s["name"], s["config"]["dlna_ip"]) for s in listed] == [
        ("Living Room", "192.168.1.20")
    ]


class FakeQobuzApi:
    """The two Qobuz API calls behind a WebSocket token: session/start and qws/createToken."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_next = 0  # HTTP status for the next token request
        self._sessions = 0
        self._tokens = 0
        app = web.Application()
        app.router.add_post("/session/start", self._start_session)
        app.router.add_post("/qws/{action}", self._token)
        self.server = TestServer(app)

    async def _start_session(self, request: web.Request) -> web.Response:
        self._sessions += 1
        self.calls.append("session/start")
        expires_at = int(time.time()) + 3600
        return web.json_response({"session_id": f"s{self._sessions}", "expires_at": expires_at})

    async def _token(self, request: web.Request) -> web.Response:
        action, body = request.match_info["action"], await request.text()
        self.calls.append(f"qws/{action} {body} {request.headers.get('X-Session-Id')}")
        if self.fail_next:
            status, self.fail_next = self.fail_next, 0
            return web.Response(status=status, text="Service Unavailable")
        if action != "createToken" or body != "jwt=jwt_qws":
            # qws/refreshToken renews the API token, which carries no WebSocket endpoint
            return web.json_response({"jwt_api": {"jwt": "api", "exp": int(time.time()) + 3600}})
        self._tokens += 1
        token = {"jwt": f"jwt{self._tokens}", "exp": int(time.time()) + 3600}
        return web.json_response({"jwt_qws": {**token, "endpoint": "wss%3A%2F%2Fqws.test%2Fws"}})


@pytest.fixture
async def qobuz_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeQobuzApi]:
    api = FakeQobuzApi()
    await api.server.start_server()
    monkeypatch.setattr(QobuzAPIClient, "API_BASE", str(api.server.make_url("")).rstrip("/"))
    try:
        yield api
    finally:
        await api.server.close()


def _logged_in_api_client() -> QobuzAPIClient:
    client = QobuzAPIClient("app-id", "app-secret")
    client.user_auth_token = "user-token"
    return client


async def test_every_qobuz_websocket_token_the_add_on_mints_comes_from_create_token(qobuz_api):
    client = _logged_in_api_client()  # shared by all speakers in the add-on

    tokens = [await client.get_ws_token() for _ in range(3)]

    assert [(t.jwt, t.endpoint) for t in tokens if t] == [
        ("jwt1", "wss://qws.test/ws"),
        ("jwt2", "wss://qws.test/ws"),
        ("jwt3", "wss://qws.test/ws"),
    ]
    assert qobuz_api.calls == ["session/start"] + ["qws/createToken jwt=jwt_qws s1"] * 3


async def test_after_a_failed_token_mint_the_next_one_starts_a_new_qobuz_session(qobuz_api):
    client = _logged_in_api_client()
    assert await client.get_ws_token() is not None

    qobuz_api.fail_next = 503  # Qobuz is having an outage
    assert await client.get_ws_token() is None
    token = await client.get_ws_token()

    assert token is not None and token.jwt == "jwt2"
    assert qobuz_api.calls == [
        "session/start",
        "qws/createToken jwt=jwt_qws s1",
        "qws/createToken jwt=jwt_qws s1",
        "session/start",
        "qws/createToken jwt=jwt_qws s2",
    ]


async def test_a_speaker_picked_again_in_the_app_rejoins_without_waiting_out_the_backoff(rig):
    # Qobuz dropped the speaker; after an outage the next try would be a minute away
    rig.ws._reconnect_delay = 60.0
    await rig.cloud.sockets[-1].close()
    await rig.wait_for(lambda: not rig.ws.is_connected, "the speaker to lose the Qobuz cloud")
    sockets = len(rig.cloud.sockets)

    # Qobuz is back and the user picks the speaker again: select() gives it 3 s to join
    await rig.select(make_tokens(session_id=rig.tokens.session_id, jwt="fresh-jwt"))

    assert len(rig.cloud.sockets) == sockets + 1
    assert rig.ws.is_renderer_active
