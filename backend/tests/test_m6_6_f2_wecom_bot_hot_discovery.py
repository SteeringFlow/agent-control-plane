"""M6.6-F2 —— WeCom Bot Hot Discovery（运行期绑定调和，只驱动 transport 生命周期）。

冻结约束在**本文件**的落点：

1. 复用 `binding_source`（此处以 `_make_source` 镜像组合根的
   `discover_bot_connections`：同 type/status 过滤 + `connection_spec_from_channel`
   的 Fail-Closed 跳过）。`discover_bot_connections` 是 `create_app` 内的闭包，
   不可 import，故镜像其**同一**读取路径；
2. 只做**调和**（ADD / KEEP / REMOVE），不新建发现 / 插件 / registry 框架；
3. `channel_id` 是绑定身份；
4. ADD / KEEP / REMOVE 三态见 hd1 / hd3 / hd2；
5. REMOVE 必须停该绑定 + 注销活连接 + 清理（`transport._bindings` 中消失）；
6. 既有绑定不产生重复线程 / 重复订阅（hd3）；
7. per-binding stop 不影响其它绑定（hd2）；
8. secret 轮换用**新** DB config，且绝不写日志（hd4）；
9. 单个非法 bot 不阻断其它 bot（hd6）；
10. single / group 语义未动（由 test_3b_3_3_wecom_bot_group_chat.py 保护，本文件不重复）；
11. **不**解决多副本 / 分布式连接所有权（Non-goal：仍是既有单实例前提）；
12. 无新框架（本文件公开面只有 `WeComBotTransport.reconcile()` / `start()` / `stop()`）；
13. 不改 Domain / DB / schema / API 契约（本文件只用既有 Domain + 既有 UoW）。

边界（刻意不覆盖）：真实企业微信联调 / 多副本连接路由 / Runtime 全链路
（后者由 test_3b_2_2 / test_3b_3_3 覆盖）。
"""

import json
import logging
import threading
import time

from sqlalchemy.orm import sessionmaker
from websockets.sync.server import serve

from app.adapters.channels.wecom_bot import (
    CHANNEL_TYPE,
    BotConnectionSpec,
    WeComBotConnectionRegistry,
    WeComBotTransport,
    WeComBotTransportConfig,
    connection_spec_from_channel,
)
from app.domain.channel import Channel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundChannelConfigError


# ================================================================== 基础设施


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _make_source(uow_factory):
    """镜像组合根 `discover_bot_connections`（同过滤 + Fail-Closed 跳过）。

    与生产**同形**：`bootstrap` 只读投影 → 全渠道 → 只留 wecom_bot + active →
    `connection_spec_from_channel`（config 不完整即抛 `InboundChannelConfigError`，
    该渠道跳过、绝不回退缺省凭据，也绝不阻断其它渠道）。
    """

    def source() -> list[BotConnectionSpec]:
        with uow_factory() as uow:
            channels = uow.channels.list_channels()
        specs: list[BotConnectionSpec] = []
        for channel in channels:
            if channel.type != CHANNEL_TYPE or channel.status != "active":
                continue
            try:
                specs.append(connection_spec_from_channel(channel))
            except InboundChannelConfigError:
                continue        # 单点配置错误：只跳过这一条
        return specs

    return source


def _create_channel(
    uow_factory, *, name: str, bot_id: str, ws_url: str,
    secret: str = "secret-1", status: str = "active", config: dict | None = None,
) -> int:
    with uow_factory() as uow:
        channel = uow.channels.create(Channel(
            name=name, type=CHANNEL_TYPE, status=status,
            config=config if config is not None
            else {"bot_id": bot_id, "bot_secret": secret, "ws_url": ws_url}))
        uow.commit()
        return channel.id


def _set_status(uow_factory, channel_id: int, status: str) -> None:
    with uow_factory() as uow:
        channel = uow.channels.get_by_id(channel_id)
        assert channel is not None
        uow.channels.update(Channel(
            name=channel.name, type=channel.type, id=channel.id,
            status=status, config=channel.config))
        uow.commit()


def _rotate_secret(uow_factory, channel_id: int, *, secret: str) -> None:
    with uow_factory() as uow:
        channel = uow.channels.get_by_id(channel_id)
        assert channel is not None
        uow.channels.update(Channel(
            name=channel.name, type=channel.type, id=channel.id,
            status=channel.status,
            config={**(channel.config or {}), "bot_secret": secret}))
        uow.commit()


def _transport(registry, source, **config) -> WeComBotTransport:
    settings = {"reconnect_backoff_s": 0.2, "recv_timeout_s": 0.2}
    settings.update(config)
    return WeComBotTransport(
        registry=registry, binding_source=source,
        on_frame=lambda spec, raw: None,
        config=WeComBotTransportConfig(**settings))


def _wait_connected(registry, channel_id: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry.is_connected(channel_id):
            return
        time.sleep(0.05)
    raise AssertionError(f"channel {channel_id} 未在 {timeout}s 内连接")


def _wait_subscribed(fake, bot_id: str, *, secret: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        subs = fake.subscribes_for(bot_id)
        if subs and subs[-1].get("body", {}).get("secret") == secret:
            return
        time.sleep(0.05)
    raise AssertionError(f"bot {bot_id} 未在 {timeout}s 内以新 secret 订阅")


class _FakeWeComWS:
    """假企业微信长连接服务端（真实 WebSocket；订阅回 errcode=0）。

    与 test_3b_2_2 的 fake 同构，但**只做连接生命周期**（订阅 / 心跳），
    足够验证「谁建了连接、用什么凭据、建了几次」。
    """

    def __init__(self) -> None:
        self.subscribes: list[dict] = []
        self._lock = threading.Lock()
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.port: int = self._server.socket.getsockname()[1]

    def _handle(self, connection) -> None:
        for raw in connection:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            req_id = (frame.get("headers") or {}).get("req_id")
            if frame.get("cmd") == "aibot_subscribe":
                with self._lock:
                    self.subscribes.append(frame)
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
            elif frame.get("cmd") == "ping":
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))

    def subscribes_for(self, bot_id: str) -> list[dict]:
        with self._lock:
            return [f for f in self.subscribes
                    if (f.get("body") or {}).get("bot_id") == bot_id]

    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        threading.Thread(
            target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()


# ================================================================== ADD


def test_hd1_add_new_bot_channel_without_restart(isolated_db_engine) -> None:
    """ADD：start() 之后新插入的 wecom_bot 渠道 → reconcile() 即建连接（无重启）。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))
        transport.start()
        try:
            _wait_connected(registry, channel_a)
            assert len(fake.subscribes_for("bot-a")) == 1

            # 运行期新增渠道（进程仍在跑）
            channel_b = _create_channel(
                uow_factory, name="b", bot_id="bot-b", ws_url=fake.ws_url())
            transport.reconcile()

            _wait_connected(registry, channel_b)
            assert registry.is_connected(channel_a)              # A 未受影响
            assert len(fake.subscribes_for("bot-b")) == 1
            assert len(fake.subscribes_for("bot-a")) == 1        # A 未重复订阅
            assert set(transport._bindings) == {channel_a, channel_b}
        finally:
            transport.stop()
    finally:
        fake.stop()


# ================================================================== REMOVE


def test_hd2_remove_bot_channel_stops_only_that_binding(isolated_db_engine) -> None:
    """REMOVE：渠道停用 → 只停**该**绑定 + 注销活连接，其它绑定不受影响。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        channel_b = _create_channel(
            uow_factory, name="b", bot_id="bot-b", ws_url=fake.ws_url())
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))
        transport.start()
        try:
            _wait_connected(registry, channel_a)
            _wait_connected(registry, channel_b)

            _set_status(uow_factory, channel_b, "inactive")
            transport.reconcile()

            # 被 REMOVE 的绑定：连接注销 + 从绑定表清理
            assert not registry.is_connected(channel_b)
            assert channel_b not in transport._bindings
            # per-binding stop：A 完全不受影响
            assert registry.is_connected(channel_a)
            assert channel_a in transport._bindings
        finally:
            transport.stop()
    finally:
        fake.stop()


# ================================================================== KEEP


def test_hd3_keep_does_not_duplicate_threads_or_subscriptions(
    isolated_db_engine,
) -> None:
    """KEEP：同 channel_id → 线程不动、订阅不重复（3 次调和仍只有一批线程）。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))
        transport.start()
        try:
            _wait_connected(registry, channel_a)
            threads_before = list(transport._bindings[channel_a].threads)
            assert len(threads_before) == 2          # rx + exec

            for _ in range(3):
                transport.reconcile()

            assert transport._bindings[channel_a].threads == threads_before
            assert all(t.is_alive() for t in threads_before)
            assert len(fake.subscribes_for("bot-a")) == 1   # 无重复订阅
            assert registry.is_connected(channel_a)
        finally:
            transport.stop()
    finally:
        fake.stop()


# ================================================================== 轮换


def test_hd4_secret_rotation_uses_fresh_db_config_without_logging_secret(
    isolated_db_engine, caplog,
) -> None:
    """secret 轮换：reconcile() 读取**新** DB config 并以新 material 重订阅；
    绝不新增线程（无重复订阅），且新旧 secret 都不进**应用日志**。

    断言只作用于应用侧 logger（`app.*`）：`websockets` 库在 DEBUG 下会把原始帧
    连同 secret 一起 trace（`protocol.py` 帧级 dump）——那是第三方库的调试面，
    不在本约束的「不记录 secret」范围内（生产不开启该库 DEBUG），故不作为判据。
    """
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url(),
            secret="secret-1")
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))
        transport.start()
        try:
            _wait_connected(registry, channel_a)
            assert fake.subscribes_for("bot-a")[0]["body"]["secret"] == "secret-1"
            threads_before = list(transport._bindings[channel_a].threads)

            with caplog.at_level(logging.DEBUG):
                _rotate_secret(uow_factory, channel_a, secret="secret-2")
                transport.reconcile()
                _wait_subscribed(fake, "bot-a", secret="secret-2")

            subs = fake.subscribes_for("bot-a")
            assert subs[-1]["body"]["secret"] == "secret-2"   # 用新 DB material
            assert len(subs) == 2                             # 恰好一次重连
            # 连接材料变更**不**新起线程（身份仍是 channel_id）
            assert transport._bindings[channel_a].threads == threads_before
            assert registry.is_connected(channel_a)
            # 凭据纪律：任何 secret 都不得出现在**应用日志**里
            app_log = "\n".join(
                record.getMessage() for record in caplog.records
                if record.name.startswith("app."))
            assert "secret-1" not in app_log
            assert "secret-2" not in app_log
        finally:
            transport.stop()
    finally:
        fake.stop()


# ================================================================== 健壮性


def test_hd5_source_error_keeps_existing_bindings(isolated_db_engine) -> None:
    """发现源整体抛错 → 保留现有绑定（绝不误拆在跑连接）。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        base = _make_source(uow_factory)
        holder = {"fail": False}

        def source() -> list[BotConnectionSpec]:
            if holder["fail"]:
                raise RuntimeError("discovery failure (simulated)")
            return base()

        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, source)
        transport.start()
        try:
            _wait_connected(registry, channel_a)

            holder["fail"] = True
            transport.reconcile()                    # 抛错 → 保留现状

            assert registry.is_connected(channel_a)
            assert channel_a in transport._bindings
        finally:
            transport.stop()
    finally:
        fake.stop()


def test_hd6_one_invalid_bot_does_not_break_valid_ones(isolated_db_engine) -> None:
    """单点非法 config（缺 bot_secret）只跳过该渠道，不阻断其它 bot。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        good = _create_channel(
            uow_factory, name="good", bot_id="bot-good", ws_url=fake.ws_url())
        bad = _create_channel(
            uow_factory, name="bad", bot_id="bot-bad", ws_url=fake.ws_url(),
            config={"bot_id": "bot-bad"})            # 缺 bot_secret → 跳过
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))
        transport.start()
        try:
            _wait_connected(registry, good)
            assert not registry.is_connected(bad)
            assert set(transport._bindings) == {good}
            assert len(fake.subscribes_for("bot-good")) == 1
            assert fake.subscribes_for("bot-bad") == []
        finally:
            transport.stop()
    finally:
        fake.stop()


def test_hd7_periodic_reconcile_discovers_new_channel(isolated_db_engine) -> None:
    """周期调和：不手动调用 reconcile()，自动发现启动后新增的渠道。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory),
                               reconcile_interval_s=0.2)
        transport.start()
        try:
            _wait_connected(registry, channel_a)
            channel_b = _create_channel(
                uow_factory, name="b", bot_id="bot-b", ws_url=fake.ws_url())
            _wait_connected(registry, channel_b)     # 无手动 reconcile
            assert len(fake.subscribes_for("bot-b")) == 1
        finally:
            transport.stop()
    finally:
        fake.stop()


def test_hd8_reconcile_is_noop_before_start_and_after_stop(
    isolated_db_engine,
) -> None:
    """生命周期守卫：未 start / 已 stop 时 reconcile() 不建任何绑定。"""
    uow_factory = _uow_factory(isolated_db_engine)
    fake = _FakeWeComWS()
    fake.start()
    try:
        channel_a = _create_channel(
            uow_factory, name="a", bot_id="bot-a", ws_url=fake.ws_url())
        registry = WeComBotConnectionRegistry()
        transport = _transport(registry, _make_source(uow_factory))

        transport.reconcile()                        # 未 start → no-op
        assert transport._bindings == {}
        assert not registry.is_connected(channel_a)

        transport.start()
        _wait_connected(registry, channel_a)
        transport.stop()

        transport.reconcile()                        # 已 stop → no-op
        assert transport._bindings == {}
        assert not registry.is_connected(channel_a)
    finally:
        fake.stop()
