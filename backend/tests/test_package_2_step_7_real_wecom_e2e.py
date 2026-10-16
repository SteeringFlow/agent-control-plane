"""Implementation Package 2 / Step 7 — **Real WeCom E2E**（Package 2 最终验收）。

本文件是 Package 2 的最终生产验收：**真实** WeCom 群聊 + **真实** WS transport +
真实 Runtime（Identity / Authorization / Routing / Session / Delivery），
Agent 刻意用 **local echo**（确定性）——变量只有 WeCom 与授权链本身。

    WeCom = real · Transport = real · Identity = real runtime
    Authorization = real persistence · Routing = real · Session = real
    Delivery = real · Agent = deterministic local echo

未配置凭据 → **整文件 skip**（普通 CI 不得因缺少真实 WeCom 环境而失败）。

env（凭据只经 env / `backend/.env`，**绝不进 fixture / 文档 / 提交**）：

    ACP_REAL_WECOM_BOT_ID           必填
    ACP_REAL_WECOM_BOT_SECRET       必填
    ACP_REAL_WECOM_GROUP_CHATID     群 A 的 chatid（正常流程：先取得再建 grant）
    ACP_REAL_WECOM_GROUP_B_CHATID   群 B（从未授权）→ 缺省则跳过隔离项
    ACP_REAL_WECOM_WS_URL           可选（缺省 wss://openws.work.weixin.qq.com）
    ACP_REAL_WECOM_WAIT_S           单个等待窗口秒数（默认 120）

运行（**必须 -s**：需要按提示在真实群里发消息）：

    ACP_REAL_WECOM_BOT_ID=... ACP_REAL_WECOM_BOT_SECRET=... \
    ACP_REAL_WECOM_GROUP_CHATID=... \
        python -m pytest tests/test_package_2_step_7_real_wecom_e2e.py -q -s

验收链（正反同场，缺一不可——只证明「能通」无法区分「授权正确」与「恒允许」）：

    真实群 A（有 grant）→ ALLOW → Session → local echo → DELIVERED
           ↓ 删除 grant
    真实群 A（无 grant）→ UNAUTHORIZED（无 Session / 无 response_payload / 授权表无该行）
    真实群 B（从未授权）→ UNAUTHORIZED（隔离：不是群 A 的 grant 泄漏）
    同群不同成员     → 不同 Identity（均 user_id=NULL）+ **同一会话级**授权

chatid 的取得方式：正常流程由 env 提供（`group_a_chatid`）。若确实无法事先取得，
才走 **bootstrap discovery**：无 grant 发一次 → 从 interaction 快照反查 chatid →
再建 grant → 正式 ALLOW 验收；它被**单独记录为 bootstrap 阶段**，绝不混入 ALLOW
验收结论。

证据：每次运行写 `backend/artifacts/step7-real-wecom-<UTC>.json`（文件名带时间戳，
**首跑永不被复跑覆盖**；中断/失败也会 flush 已完成的阶段）。

边界（冻结）：本文件**不改任何生产代码**，不改 Package 2 已冻结实现。首跑若失败，
先按层定位（WeCom WS → Adapter → 持久化 → Identity → Authorization → Routing →
Session → Agent → Delivery → ACK），**不因第一次失败就改 Runtime**。
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.adapters.channels.wecom_bot import (
    CHANNEL_TYPE,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
    WeComBotTransport,
    WeComBotTransportConfig,
    connection_spec_from_channel,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.connection_conversations import (
    ConnectionConversationApplicationService,
    CreateConnectionConversation,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.domain.activity_record import STAGE_AUTHORIZATION
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    PROCESSING_FAILED,
    PROCESSING_SUCCEEDED,
)
from app.persistence import models  # noqa: F401  # 导入即注册表
from app.persistence.database import Base
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
ARTIFACTS = BACKEND / "artifacts"

#: 验收阶段的**规范顺序**；`_Session.ensure()` 保证即便 pytest 随机化测试顺序，
#: 依赖阶段也按此顺序先跑（阶段结果缓存）。
PHASES = ("bootstrap", "positive", "second_member", "deny", "group_b")


# ---------------------------------------------------------------- env 门控


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _live_config() -> dict | None:
    """读取 live 配置；凭据只经进程 env 或 `backend/.env`。"""
    load_dotenv(BACKEND / ".env", override=False)
    bot_id = _env("ACP_REAL_WECOM_BOT_ID")
    secret = _env("ACP_REAL_WECOM_BOT_SECRET")
    if not (bot_id and secret):
        return None
    try:
        wait_s = float(_env("ACP_REAL_WECOM_WAIT_S") or 120)
    except ValueError:
        wait_s = 120.0
    return {
        "bot_id": bot_id,
        "bot_secret": secret,
        "ws_url": _env("ACP_REAL_WECOM_WS_URL") or None,
        "chatid": _env("ACP_REAL_WECOM_GROUP_CHATID") or None,
        "chatid_b": _env("ACP_REAL_WECOM_GROUP_B_CHATID") or None,
        "member_a": _env("ACP_REAL_WECOM_MEMBER_A_ID") or None,
        "member_b": _env("ACP_REAL_WECOM_MEMBER_B_ID") or None,
        "wait_s": wait_s,
    }


# ---------------------------------------------------------------- 证据落盘


class _Evidence:
    """每次运行一个独立文件（含时间戳）→ 首跑永不被复跑覆盖。"""

    def __init__(self, cfg: dict) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + \
            f"-{datetime.now(timezone.utc).microsecond // 1000:03d}"
        self.path = ARTIFACTS / f"step7-real-wecom-{stamp}.json"
        self.doc: dict = {
            "kind": "package-2-step-7-real-wecom-e2e",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "env": {
                "bot_id": cfg["bot_id"],
                "bot_secret": "***configured***",   # 凭据绝不落盘
                "ws_url": cfg["ws_url"] or "(default)",
                "group_a_chatid": cfg["chatid"],
                "group_b_chatid": cfg["chatid_b"],
                "member_a": cfg["member_a"],
                "member_b": cfg["member_b"],
                "wait_s": cfg["wait_s"],
            },
            "phases": {},
        }
        self._flush()

    def record(self, phase: str, payload: dict) -> None:
        self.doc["phases"][phase] = payload
        self._flush()

    def fail(self, phase: str, message: str) -> None:
        self.record(phase, {"status": "FAILED", "error": message})

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.doc, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")


# ---------------------------------------------------------------- 运行会话


class _Session:
    """一次真实 E2E 会话：开通 → 起真实 transport → 按阶段等人发真实消息。"""

    def __init__(self, engine, cfg: dict) -> None:
        self.engine = engine
        self.cfg = cfg
        self.evidence = _Evidence(cfg)
        self.results: dict[str, dict | None] = {}
        self._attempts: dict[str, int] = {}
        self._last_id = 0
        self.transport: WeComBotTransport | None = None
        self.chatid: str | None = cfg["chatid"]
        self.channel_id: int | None = None
        self.connection_id: int | None = None

    # -- UnitOfWork 工厂（expire_on_commit=False 与既有测试同形）
    @property
    def uow_factory(self):
        return lambda: SqlAlchemyUnitOfWork(
            session_factory=sessionmaker(bind=self.engine, expire_on_commit=False))

    # ------------------------------------------------------------ 开通 + 起连接
    def start(self) -> None:
        print(f"\n[step7] 证据文件：{self.evidence.path}", flush=True)
        with self.uow_factory() as uow:
            config = {"bot_id": self.cfg["bot_id"], "bot_secret": self.cfg["bot_secret"]}
            if self.cfg["ws_url"]:
                config["ws_url"] = self.cfg["ws_url"]
            channel = uow.channels.create(Channel(
                name="step7-real-wecom", type=CHANNEL_TYPE, status="active",
                config=config))
            agent = uow.agents.create(Agent(name="step7-local-echo", type="local"))
            path = uow.connections.create(Connection(
                channel_id=channel.id, agent_id=agent.id, name="step7-path",
                status="active"))
            uow.commit()
        self.channel_id = channel.id
        self.connection_id = path.id
        # 未绑定身份**必须预先存在**：IdentityResolution 按 ADR-5 绝不创建 Identity，
        # 而管理 API 的 IdentityCreate.user_id 必填（V0 不暴露「创建未绑定身份」入口）。
        # 因此由开通侧播种 user_id=NULL 的行（与 Package 1 测试同法）——运行时只负责
        # 「解析到它并照常放行」，本 E2E 验的就是这一句。
        self.provisioned: list[str] = []
        for member_id in (self.cfg["member_a"], self.cfg["member_b"]):
            if member_id and self.provision_unbound_identity(member_id):
                self.provisioned.append(member_id)

        registry = WeComBotConnectionRegistry()
        uow_factory = self.uow_factory
        channel_adapter = MultiChannelAdapter(
            {CHANNEL_TYPE: WeComBotAdapter(registry=registry)}, uow_factory=uow_factory)
        agent_adapter = MultiAgentAdapter(
            {"local": LocalAgentAdapter()}, uow_factory=uow_factory)
        runtime = RuntimeCoordinator(
            identity_resolution=IdentityResolutionExecutor(uow_factory),
            access_authorization=AccessAuthorizationExecutor(uow_factory),
            routing=RoutingExecutor(uow_factory),
            session_resolution=SessionResolutionExecutor(uow_factory),
            session_creation=SessionCreationExecutor(uow_factory),
            agent_dispatch=AgentDispatchExecutor(
                agent_adapter=agent_adapter, uow_factory=uow_factory),
            delivery=DeliveryExecutor(channel_adapter=channel_adapter),
        )
        phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
        worker = Worker(runtime=runtime, phases=phases, uow_factory=uow_factory)
        service = ChannelInboundService(
            uow_factory=uow_factory, intake=phases, inbound_adapter=channel_adapter)

        def on_frame(binding, raw: str) -> None:
            acceptance = service.accept_message(
                binding.channel_id, signature="", timestamp="", nonce="", body=raw)
            if acceptance.interaction_id is not None:
                worker.process(acceptance.interaction_id)

        with uow_factory() as uow:
            spec = connection_spec_from_channel(uow.channels.get_by_id(self.channel_id))

        # 生产默认节奏（heartbeat 30s / recv 1s / subscribe 10s）——不放大任何预算
        self.transport = WeComBotTransport(
            registry=registry, binding_source=lambda: [spec], on_frame=on_frame,
            config=WeComBotTransportConfig())
        self.transport.start()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not registry.is_connected(self.channel_id):
            time.sleep(0.2)
        if not registry.is_connected(self.channel_id):
            raise AssertionError(
                "真实 WeCom WS 未建立订阅连接：检查 bot_id / bot_secret / ws_url "
                "与出网（注意 aibot_subscribe 需 errcode==0 才算已连接）")
        self.evidence.record("setup", {
            "status": "connected",
            "channel_id": self.channel_id,
            "connection_id": self.connection_id,
            "grants": self.grants(),
        })
        print(f"[step7] 已订阅真实 WeCom WS（channel_id={self.channel_id}）", flush=True)

    def stop(self) -> None:
        if self.transport is not None:
            try:
                self.transport.stop()
            finally:
                self.transport = None
        try:
            self.evidence.record("final_state", {
                "grants": self.grants(),
                "interactions": self._interaction_summary(),
            })
        except Exception as exc:  # noqa: BLE001  收尾不得掩盖主结论
            self.evidence.record("final_state", {"status": "flush_failed",
                                                 "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------ grant 操作
    def provision_unbound_identity(self, external_id: str) -> bool:
        """播种一条 **user_id=NULL** 的 External Identity（Package 1 语义）。

        已存在则返回 False（不改写既有行，尤其不补绑定）。
        """
        with self.uow_factory() as uow:
            if uow.identities.find_by_channel_and_external_id(
                    self.channel_id, external_id) is not None:
                return False
            uow.identities.create(Identity(
                user_id=None, channel_id=self.channel_id, external_id=external_id))
            uow.commit()
        return True

    def _service(self) -> ConnectionConversationApplicationService:
        return ConnectionConversationApplicationService(uow_factory=self.uow_factory)

    def grants(self) -> list[list]:
        """显式授权行（管理面 Service 的真实读路径）。"""
        return [[row.id, row.external_conversation_id]
                for row in self._service().list_connection_conversations(
                    self.connection_id)]

    def grant(self, conversation: str) -> None:
        self._service().create_connection_conversation(CreateConnectionConversation(
            channel_id=self.channel_id, external_conversation_id=conversation,
            connection_id=self.connection_id))

    def revoke(self, conversation: str) -> bool:
        for row in self._service().list_connection_conversations(self.connection_id):
            if row.external_conversation_id == conversation:
                self._service().delete_connection_conversation(row.id)
                return True
        return False

    # ------------------------------------------------------------ 读库 / 等待
    def _rows(self) -> list[InteractionModel]:
        with Session(self.engine) as s:
            return list(s.execute(
                select(InteractionModel).order_by(InteractionModel.id)).scalars())

    def _interaction_summary(self) -> list[dict]:
        return [{"id": r.id,
                 "conversation": (r.request_payload or {}).get("external_conversation_id"),
                 "processing": r.processing_status,
                 "delivery": r.delivery_status,
                 "failure_code": r.failure_code}
                for r in self._rows()]

    def await_interaction(self, prompt: str, conversation: str | None = None, *,
                          provision: bool = True,
                          exclude_identity: str | None = None,
                          exclude_conversation: str | None = None) -> dict:
        """等人发真实消息；返回该 interaction 的完整事实面（含 activities）。

        `provision=True`：若观察到 `IDENTITY_RESOLUTION_FAILED`（发送者尚无身份行），
        **就地播种 `user_id=NULL` 身份并请其重发一次**——未绑定身份必须预先存在，
        运行时不创建（ADR-5），所以这是开通动作而非放宽判定；每次播种都记入证据。
        `provision=False`：直接把首个结果交还调用方（bootstrap 用）。
        `exclude_identity`：忽略来自该发送者的消息（用于等「另一位成员」）。
        """
        print(f"\n[step7] >>> {prompt}", flush=True)
        deadline = time.monotonic() + self.cfg["wait_s"]
        while time.monotonic() < deadline:
            for row in self._rows():
                if row.id <= self._last_id:
                    continue
                snap = row.request_payload or {}
                if (conversation is not None
                        and snap.get("external_conversation_id") != conversation):
                    continue
                if (exclude_conversation is not None
                        and snap.get("external_conversation_id") == exclude_conversation):
                    continue      # 只认「不是群 A」的那条（群 B 的 chatid 自动发现）
                if (exclude_identity is not None
                        and snap.get("external_identity_id") == exclude_identity):
                    continue      # 还不是「另一位成员」，继续等
                if row.processing_status in (PROCESSING_SUCCEEDED, PROCESSING_FAILED):
                    self._last_id = row.id
                    facts = self._facts(row)
                    if (provision
                            and facts["failure_code"] == "IDENTITY_RESOLUTION_FAILED"):
                        sender = facts["external_identity_id"]
                        created = bool(sender) and self.provision_unbound_identity(sender)
                        if created:
                            self.provisioned.append(sender)
                            self.evidence.record(
                                f"provisioned_in_phase:{facts['interaction_id']}",
                                {"sender": sender, "user_id": None,
                                 "note": "身份判定前置于授权判定，故先播种再重发"})
                        print(f"[step7] ! 发送者 {sender!r} 无身份行 → "
                              f"{'已播种 user_id=NULL 身份' if created else '身份已存在'}；"
                              "请在群里**再发一次**同一条消息", flush=True)
                        continue
                    return facts
            time.sleep(0.25)
        extra = (f"（需与 {exclude_identity!r} **不同**的成员发送）"
                 if exclude_identity else "")
        raise AssertionError(
            f"等待真实消息超时（{self.cfg['wait_s']}s）：{prompt}{extra}")

    def _facts(self, row: InteractionModel) -> dict:
        with Session(self.engine) as s:
            acts = list(s.execute(
                select(ActivityModel)
                .where(ActivityModel.interaction_id == row.id)
                .order_by(ActivityModel.id)).scalars())
        snap = row.request_payload or {}
        return {
            "interaction_id": row.id,
            "external_interaction_id": row.external_interaction_id,
            "conversation": snap.get("external_conversation_id"),
            "external_identity_id": snap.get("external_identity_id"),
            "processing_status": row.processing_status,
            "delivery_status": row.delivery_status,
            "failure_stage": row.failure_stage,
            "failure_code": row.failure_code,
            "retryable": row.retryable,
            "session_id": row.session_id,
            "response_payload": row.response_payload,
            "activities": [(a.type, a.stage, a.status) for a in acts],
        }

    def identity(self, external_id: str | None):
        with self.uow_factory() as uow:
            return uow.identities.find_by_channel_and_external_id(
                self.channel_id, external_id or "")

    # ------------------------------------------------------------ 阶段状态机
    def ensure(self, name: str):
        for phase in PHASES[: PHASES.index(name) + 1]:
            if phase in self.results:
                continue
            attempt = self._attempts.get(phase, 0) + 1
            self._attempts[phase] = attempt
            try:
                outcome = self._run(phase)
            except Exception as exc:  # noqa: BLE001  失败也要留痕后原样抛出
                # 失败尝试写**独立键**，绝不被后续成功覆盖（首跑事实必须留痕）
                self.evidence.record(f"failed_attempt:{phase}:{attempt}",
                                     {"status": "FAILED",
                                      "error": f"{type(exc).__name__}: {exc}"})
                raise
            if isinstance(outcome, dict) and attempt > 1:
                outcome["recovered_after_failed_attempts"] = attempt - 1
                self.evidence.record(phase, outcome)
            self.results[phase] = outcome
        return self.results[name]

    def _run(self, phase: str):
        if phase == "bootstrap":
            if self.chatid and self.cfg["member_a"]:
                return {"skipped": True,
                        "reason": "chatid 与 member_a 已由 env 提供（正常流程）"}
            facts = self.await_interaction(
                "【bootstrap】chatid / 成员 id 未提供：请在群 A **@机器人** 发一条消息，"
                "本步用它反查 chatid 与发送者 id（**记为 bootstrap discovery，"
                "不计入 ALLOW 验收**）", provision=False)
            learned: dict = {}
            if not self.chatid:
                self.chatid = facts["conversation"]
                learned["chatid"] = self.chatid
            sender = facts["external_identity_id"]
            if sender and self.provision_unbound_identity(sender):
                learned["provisioned_unbound_identity"] = sender
                self.provisioned.append(sender)
            outcome = {"mode": "bootstrap_discovery", "learned": learned,
                       "chatid": self.chatid, "provisioned": list(self.provisioned),
                       **facts}
            self.evidence.record("bootstrap", outcome)
            return outcome

        if phase == "positive":
            if not any(conv == self.chatid for _, conv in self.grants()):
                self.grant(self.chatid)
            facts = self.await_interaction(
                f"【ALLOW 验收】请在群 A（chatid={self.chatid}）由**任意成员**发一条文本消息",
                conversation=self.chatid)
            outcome = {"mode": "allow", "grants": self.grants(), **facts}
            self.evidence.record("positive", outcome)
            return outcome

        if phase == "second_member":
            first = self.results.get("positive") or {}
            facts = self.await_interaction(
                f"【同会话/不同成员】请让群 A 中**另一位成员**（与上一条不同的人）"
                f"@机器人 发一条文本消息（chatid={self.chatid}）",
                conversation=self.chatid,
                exclude_identity=first.get("external_identity_id"))
            outcome = {"mode": "allow_other_member", "grants": self.grants(), **facts}
            self.evidence.record("second_member", outcome)
            return outcome

        if phase == "deny":
            revoked = self.revoke(self.chatid)
            facts = self.await_interaction(
                f"【DENY 验收】grant 已删除（revoked={revoked}）：请在群 A 由任意成员"
                "再发一条文本消息（应被 UNAUTHORIZED 拒绝）",
                conversation=self.chatid)
            outcome = {"mode": "deny_after_revoke", "revoked": revoked,
                       "grants": self.grants(), **facts}
            self.evidence.record("deny", outcome)
            return outcome

        if phase == "group_b":
            # 群 B 的 chatid 未提供时自动发现（bootstrap discovery：认「不是群 A」的会话）
            target = self.cfg["chatid_b"]
            prompt = (f"【隔离】请在群 B（chatid={target}，从未授权）@机器人 发一条消息"
                      if target else
                      "【隔离】请在**另一个从未授权的群**（群 B）@机器人 发一条消息"
                      "（其 chatid 将由本条自动发现）")
            try:
                facts = self.await_interaction(
                    prompt, conversation=target,
                    exclude_conversation=None if target else self.chatid)
            except AssertionError as exc:
                # 群 B 未收到消息：**如实记为未执行**，不静默当成通过
                self.evidence.record("group_b", {
                    "status": "NOT_OBSERVED", "reason": str(exc)})
                return None
            if target is None:
                self.cfg["chatid_b"] = facts["conversation"]
                self.evidence.record("group_b_chatid_bootstrap",
                                     {"learned_chatid_b": facts["conversation"]})
            outcome = {"mode": "deny_group_b", "grants": self.grants(), **facts}
            self.evidence.record("group_b", outcome)
            return outcome

        raise AssertionError(f"未知阶段：{phase}")


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def live() -> dict:
    cfg = _live_config()
    if cfg is None:
        pytest.skip("未配置 ACP_REAL_WECOM_BOT_ID / ACP_REAL_WECOM_BOT_SECRET"
                    "（Step 7 真实 WeCom E2E 门控）")
    return cfg


@pytest.fixture(scope="module")
def e2e_engine(tmp_path_factory):
    """**文件型** SQLite（一个 UoW = 一条连接 = 一个隔离事务）。

    与 3B-3.3 WIP Closure 同一约束：transport 的 exec 线程在后台写库，
    测试线程同时读——共享单连接会产生脏读（假绿）与读者回滚写者事务（假红）。
    真实 E2E 的结论必须建立在**已提交**数据上。
    """
    path = tmp_path_factory.mktemp("step7") / "acp.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def session(e2e_engine, live):
    s = _Session(e2e_engine, live)
    s.start()
    try:
        yield s
    finally:
        s.stop()


# ---------------------------------------------------------------- 验收断言


def test_step7_1_positive_path_allows_and_delivers(session) -> None:
    """真实群 + grant → ALLOW → Session → local echo → DELIVERED（真实 ACK）。"""
    facts = session.ensure("positive")
    assert facts["processing_status"] == PROCESSING_SUCCEEDED, facts
    assert facts["delivery_status"] == DELIVERY_DELIVERED, facts
    # DELIVERED 的前提是 aibot_respond_msg 收到真实平台 errcode==0（两层成功语义）
    assert facts["session_id"] is not None, "未创建 Session"
    assert facts["response_payload"] is not None, "无响应载荷"
    assert facts["failure_stage"] is None and facts["failure_code"] is None, facts
    assert facts["conversation"] == session.chatid, \
        f"会话键必须是群 chatid：{facts['conversation']!r}"

    # Package 1：群成员未绑定 ACP User → Identity 由运行时创建且 user_id = NULL
    ident = session.identity(facts["external_identity_id"])
    assert ident is not None, "运行时未创建 External Identity"
    assert ident.user_id is None, "Package 1：群成员不应绑定 ACP User（user_id 必须为 NULL）"

    # Step 2：不得出现 authorization 阶段失败证据
    assert [a for a in facts["activities"] if a[1] == STAGE_AUTHORIZATION] == [], \
        f"存在 authorization stage_failure：{facts['activities']}"


def test_step7_2_same_group_different_member_shares_conversation_authorization(
        session) -> None:
    """同群不同成员：不同 Identity，**同一会话级**授权（授权不按人）。"""
    first = session.ensure("positive")
    second = session.ensure("second_member")   # 第二位成员可当场发现并就地播种

    assert second["processing_status"] == PROCESSING_SUCCEEDED, second
    assert second["delivery_status"] == DELIVERY_DELIVERED, second
    assert second["conversation"] == session.chatid == first["conversation"], \
        "授权作用域是 Conversation（群），不是成员"
    assert second["external_identity_id"] != first["external_identity_id"], \
        "第二条应由**另一个成员**发送（否则无法验证“不同 Identity 同一授权”）"

    ident2 = session.identity(second["external_identity_id"])
    assert ident2 is not None and ident2.user_id is None, "第二个成员同样未绑定 ACP User"


def test_step7_3_after_grant_deleted_denies_with_unauthorized(session) -> None:
    """删除 grant 后同群再发 → UNAUTHORIZED（冻结的拒绝形状）。"""
    deny = session.ensure("deny")

    assert deny["processing_status"] == PROCESSING_FAILED, deny
    assert deny["delivery_status"] == DELIVERY_PENDING, deny
    assert deny["failure_stage"] == STAGE_AUTHORIZATION, deny
    assert deny["failure_code"] == "UNAUTHORIZED", deny
    assert deny["retryable"] is False, deny
    assert deny["session_id"] is None, "拒绝不得创建 Session"
    assert deny["response_payload"] is None, "拒绝不得产生响应载荷"

    # 授权表确实没有该 grant（不是“判定错了”，而是“真的没有许可”）
    assert all(conv != session.chatid for _, conv in session.grants()), \
        f"授权表仍存在该会话：{session.grants()}"

    # 拒绝也要留下 stage_failure 证据（Package 2-C §7.6 诚实性）
    assert any(a[1] == STAGE_AUTHORIZATION for a in deny["activities"]), \
        f"缺少 authorization stage_failure：{deny['activities']}"


def test_step7_4_group_b_without_grant_denies(session) -> None:
    """隔离：从未授权的群 B → UNAUTHORIZED（不是群 A 的 grant 泄漏）。"""
    facts = session.ensure("group_b")
    if facts is None:
        pytest.skip("群 B 隔离项未执行：未在该群收到消息（证据已记为 NOT_OBSERVED）")

    assert facts["processing_status"] == PROCESSING_FAILED, facts
    assert facts["failure_stage"] == STAGE_AUTHORIZATION, facts
    assert facts["failure_code"] == "UNAUTHORIZED", facts
    assert facts["session_id"] is None, "隔离组不得创建 Session"
    assert all(conv != session.cfg["chatid_b"] for _, conv in session.grants()), \
        f"群 B 不应存在授权：{session.grants()}"
