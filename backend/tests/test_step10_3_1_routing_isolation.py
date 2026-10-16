"""Step 10.3.1 Layer A —— Routing isolation（多 Channel → 多 Agent 边界验证）。

模型（10.3.1 §第一阶段：不是"多 Agent 于一个 Channel"，而是 Routing Boundary）：

    Channel-1 ── Connection-1 ── Agent-1
    Channel-2 ── Connection-2 ── Agent-2

断言：channel 1 → agent 1、channel 2 → agent 2，且两者互不串扰。
Runtime Core / RoutingExecutor / Port **零改动**——本层只观察既有行为。

**Routing Capability Gap（10.3.1 记录，不修）**：单 Channel 下多个 ACTIVE
Connection 会被既有 Fail Closed 拒绝（数量 != 1 → RoutingFailedError）——
这是 8.4 冻结的语义（一个 Channel 对应一个 Active Agent），不是 bug；
「一个 Channel 连接多个 Agent」是未来产品决策，进入下一 Step。
"""

import pytest
from sqlalchemy.orm import sessionmaker

from app.application.errors import RoutingFailedError
from app.application.routing import RoutingExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_two_paths(db_engine) -> dict[str, int]:
    """两条完整 Access Path：Channel-1→Conn-1→Agent-1 / Channel-2→Conn-2→Agent-2。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        ch_a = uow.channels.create(Channel(name="wecom-a", type="wecom"))
        ch_b = uow.channels.create(Channel(name="wecom-b", type="wecom"))
        agent_a = uow.agents.create(Agent(name="agent-a", type="acp"))
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=ch_a.id, external_id="wx_a"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=ch_b.id, external_id="wx_b"))
        conn_a = uow.connections.create(Connection(
            channel_id=ch_a.id, agent_id=agent_a.id, name="path-a",
            status="active", config={"endpoint": "http://agent-a.internal"},
            credential_ref="cred-a"))
        conn_b = uow.connections.create(Connection(
            channel_id=ch_b.id, agent_id=agent_b.id, name="path-b",
            status="active", config={"endpoint": "http://agent-b.internal"},
            credential_ref="cred-b"))
        uow.commit()
        return {"channel_a": ch_a.id, "channel_b": ch_b.id,
                "conn_a": conn_a.id, "conn_b": conn_b.id,
                "agent_a": agent_a.id, "agent_b": agent_b.id}


@pytest.fixture()
def paths(db_engine):
    return _seed_two_paths(db_engine)


def _routing(db_engine) -> RoutingExecutor:
    return RoutingExecutor(uow_factory=_uow_factory(db_engine))


# ================================================================== A1/A2 正确路由与派生


def test_a1_channel_a_routes_to_agent_a(paths, db_engine) -> None:
    result = _routing(db_engine).execute(
        paths["channel_a"], frozenset({paths["conn_a"]}))
    assert result.connection_id == paths["conn_a"]
    assert result.agent_id == paths["agent_a"]        # 派生自 Connection，不另选


def test_a2_channel_b_routes_to_agent_b(paths, db_engine) -> None:
    result = _routing(db_engine).execute(
        paths["channel_b"], frozenset({paths["conn_b"]}))
    assert result.connection_id == paths["conn_b"]
    assert result.agent_id == paths["agent_b"]


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_a3_isolated_under_any_call_order(paths, db_engine, order) -> None:
    """两条路径完全独立：任何调用顺序下结果一一对应、互不污染。"""
    routing = _routing(db_engine)
    results = {}
    for key in order:
        results[key] = routing.execute(
            paths[f"channel_{key}"], frozenset({paths[f"conn_{key}"]}))
    assert results["a"].connection_id == paths["conn_a"]
    assert results["a"].agent_id == paths["agent_a"]
    assert results["b"].connection_id == paths["conn_b"]
    assert results["b"].agent_id == paths["agent_b"]


def test_a4_connection_id_and_agent_id_are_derived_not_chosen(paths, db_engine) -> None:
    """Routing 只选 Connection；agent_id 严格 = 所选 Connection.agent_id。

    把 Agent A/B 的 id 对调语义（用一个 agent 挂两条 channel 也可成立）——
    这里验证的是派生关系本身：result.agent_id 必须能在库中由
    connection.agent_id 复核。
    """
    routing = _routing(db_engine)
    with _uow_factory(db_engine)() as uow:
        for channel_key, conn_key in (("channel_a", "conn_a"),
                                      ("channel_b", "conn_b")):
            result = routing.execute(paths[channel_key],
                                     frozenset({paths[conn_key]}))
            stored = uow.connections.get_by_id(result.connection_id)
            assert stored is not None
            assert result.agent_id == stored.agent_id


# ================================================================== A3' Capability Gap（记录，不修）


def test_a5_single_channel_with_two_active_connections_still_fails_closed(
        db_engine) -> None:
    """Routing Capability Gap 正验证：一个 Channel 挂两个 ACTIVE Connection
    → RoutingFailedError（8.4 冻结语义；10.3.1 不放松、不扩 Routing）。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="bob"))
        channel = uow.channels.create(Channel(name="wecom-multi", type="wecom"))
        agent_1 = uow.agents.create(Agent(name="hr-agent", type="acp"))
        agent_2 = uow.agents.create(Agent(name="sales-agent", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_m"))
        path_1 = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_1.id, name="path-1",
            status="active", credential_ref="cred-1"))
        path_2 = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_2.id, name="path-2",
            status="active", credential_ref="cred-2"))
        uow.commit()
        channel_id = channel.id
        # Package 2 / Step 2：两条都给许可——失败必须来自「候选 >1」，
        # 不能来自「候选为空」（否则测试会以错误的理由通过）
        granted = frozenset({path_1.id, path_2.id})
    with pytest.raises(RoutingFailedError):
        _routing(db_engine).execute(channel_id, granted)


def test_a6_fail_closed_regression_zero_and_unknown(paths, db_engine) -> None:
    """既有 Fail Closed 回归：未知 channel / 无 active Connection 不猜。"""
    routing = _routing(db_engine)
    with pytest.raises(RoutingFailedError):
        routing.execute(99999, frozenset())
