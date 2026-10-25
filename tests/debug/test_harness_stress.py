"""Adversarial harness checks through real readers and independent processes.

No hardware or model provider is involved. Each subprocess inherits pytest's
isolated PHYSICLAW_HOME; production code and the file locks are used unchanged.
"""

import json
import os
import subprocess
import sys
import time
from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common import paths
from physiclaw.common.listing import Screen, decode_elements
from physiclaw.conductor.spec import reply
from physiclaw.debug import thread as vthread

ASK = "准备测试。回复 确认 开始，或 取消。"
YES = frozenset({"确认", "好的", "ok"})
NO = frozenset({"取消", "不用", "no"})


def _read(bubbles, incoming=reply.INCOMING_LEFT):
    return Screen.read(vthread.render_listing(bubbles, None, incoming))


@pytest.mark.parametrize("incoming", [reply.INCOMING_LEFT, (0.55, 0, 1, 1)])
@pytest.mark.parametrize("separator", ["\n", "\r\n", "\u2028", "\n\n"])
@pytest.mark.parametrize(
    "answer", ["先不要执行|确认", "修改任务后再做|好的", "取消|确认", "确认|取消"]
)
def test_multiline_reply_is_one_message_not_approval_of_its_last_line(
    incoming, separator, answer
):
    before = [vthread.Bubble(vthread.USER, "测试"), vthread.Bubble(vthread.AGENT, ASK)]
    baseline = {r.label for r in _read(before, incoming).rows}
    text = answer.replace("|", separator)
    screen = _read([*before, vthread.Bubble(vthread.USER, text)], incoming)

    messages, placed = reply.read_incoming(
        screen.rows, baseline, ASK, incoming=incoming
    )

    assert placed
    assert messages == [answer.replace("|", " ")]
    assert reply.classify_all(messages, YES, NO) is None


def test_separate_bubbles_still_allow_a_changed_mind():
    before = [vthread.Bubble(vthread.AGENT, ASK)]
    screen = _read(
        [
            *before,
            vthread.Bubble(vthread.USER, "取消"),
            vthread.Bubble(vthread.USER, "确认"),
        ]
    )
    messages, placed = reply.read_incoming(
        screen.rows, set(), ASK, incoming=reply.INCOMING_LEFT
    )
    assert placed and messages == ["取消", "确认"]
    assert reply.classify_all(messages, YES, NO) is reply.Answer.CONFIRM


@pytest.mark.slow
@settings(max_examples=1000, deadline=None)
@given(
    lines=st.lists(
        st.sampled_from(
            ["先不要执行", "请修改任务", "稍后再做", "确认", "取消", "好的", "ok"]
        ),
        min_size=2,
        max_size=8,
    ),
    history=st.integers(min_value=0, max_value=40),
    incoming=st.sampled_from([reply.INCOMING_LEFT, (0.55, 0, 1, 1)]),
)
def test_wrapped_reply_preserves_whole_message_semantics(lines, history, incoming):
    before = [vthread.Bubble(vthread.USER, f"旧消息{i}") for i in range(history)]
    before.append(vthread.Bubble(vthread.AGENT, ASK))
    baseline = {r.label for r in _read(before, incoming).rows}
    text = "\n".join(lines)
    listing = vthread.render_listing(
        [*before, vthread.Bubble(vthread.USER, text)], None, incoming
    )
    rows = tuple(decode_elements(listing))
    messages, placed = reply.read_incoming(rows, baseline, ASK, incoming=incoming)

    assert placed and messages == [" ".join(lines)]
    assert reply.classify_all(messages, YES, NO) == reply.classify(text, YES, NO)
    assert all(0 <= coordinate <= 1 for row in rows for coordinate in row.bbox)


@pytest.mark.slow
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    operations=st.lists(
        st.tuples(
            st.sampled_from(["stage", "send", "peek", "roundtrip"]),
            st.text(max_size=100),
        ),
        min_size=20,
        max_size=80,
    )
)
def test_scripted_conversation_conserves_replies_across_sequences(operations):
    vthread.seed("task", [])
    pending = []
    expected = [(vthread.USER, "task")]
    for operation, text in operations:
        if operation == "stage":
            pending.append(text)
            vthread.stage([text])
        elif operation == "send":
            expected.append((vthread.AGENT, text))
            vthread.record_send(text)
        elif operation == "peek":
            if pending and expected[-1][0] == vthread.AGENT:
                expected.append((vthread.USER, pending.pop(0)))
            vthread.peek_bubbles()
        else:
            # Persist the reloaded snapshot.
            vthread.save(vthread.load())
        state = vthread.load()
        assert [(bubble.sender, bubble.text) for bubble in state.bubbles] == expected
        assert state.staged == pending


def _workers(code, arguments):
    env = {**os.environ, "PHYSICLAW_HOME": str(paths.HOME)}
    barrier = paths.debug_dir() / "stress-race"
    barrier.mkdir(parents=True, exist_ok=True)
    # Import before the barrier so import latency cannot serialize the race.
    synchronized = """
import os, time
from physiclaw.common import paths
from physiclaw.debug import thread
from physiclaw.agent.hooks.debug import debug_wake
barrier = paths.debug_dir() / 'stress-race'
(barrier / ('ready-' + os.environ['HARNESS_WORKER'])).touch()
deadline = time.monotonic() + 30
while not (barrier / 'go').exists():
    assert time.monotonic() < deadline, 'parent did not release race barrier'
    time.sleep(.001)
"""
    processes = []
    try:
        for index, args in enumerate(arguments):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", synchronized + code, *args],
                    env={**env, "HARNESS_WORKER": str(index)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 20
        while len(list(barrier.glob("ready-*"))) != len(processes):
            assert all(p.poll() is None for p in processes), "race worker exited"
            assert time.monotonic() < deadline, "race workers did not become ready"
            time.sleep(0.01)
        (barrier / "go").touch()
        outputs = []
        for p in processes:
            stdout, stderr = p.communicate(timeout=45)
            assert p.returncode == 0, stderr
            outputs.append(stdout)
        return outputs
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)


@pytest.mark.slow
def test_competing_processes_do_not_lose_or_duplicate_messages():
    vthread.seed("task", [])
    _workers(
        """
import sys
kind, worker = sys.argv[1:]
for i in range(50):
    text = f'{worker}:{i}:消息'
    if kind == 'stage':
        thread.stage([text])
    elif kind == 'send':
        thread.record_send(text)
    else:
        thread.peek_bubbles()
""",
        [(kind, str(i)) for kind in ("stage", "send", "peek") for i in range(4)],
    )
    state = vthread.load()
    expected = Counter(f"{i}:{j}:消息" for i in range(4) for j in range(50))
    agents = [b.text for b in state.bubbles if b.sender == vthread.AGENT]
    released = [b.text for b in state.bubbles[1:] if b.sender == vthread.USER]
    user_replies = [*released, *state.staged]
    assert Counter(agents) == expected
    assert Counter(user_replies) == expected
    # Writers may interleave, but an individual writer's FIFO must survive.
    for worker in range(4):
        for values in (agents, user_replies):
            numbers = [
                int(s.split(":")[1]) for s in values if s.startswith(f"{worker}:")
            ]
            assert numbers == list(range(50))


@pytest.mark.slow
def test_competing_peeks_release_one_reply_for_one_send():
    vthread.seed("task", ["确认", "取消", "稍后"])
    vthread.record_send(ASK)
    _workers(
        "thread.peek_bubbles()",
        [() for _ in range(12)],
    )
    state = vthread.load()
    assert [b.text for b in state.bubbles] == ["task", ASK, "确认"]
    assert state.staged == ["取消", "稍后"]


@pytest.mark.slow
def test_wake_has_one_consumer_across_processes(monkeypatch):
    from physiclaw.agent.hooks.debug import wake_path
    from physiclaw.common.config import DEBUG_ENV_VAR

    monkeypatch.setenv(DEBUG_ENV_VAR, "1")
    wake_path().parent.mkdir(parents=True, exist_ok=True)
    wake_path().write_text(json.dumps({"description": "stress wake"}), encoding="utf-8")
    results = _workers(
        """
wake = debug_wake()
print(wake.description if wake else '-')
""",
        [() for _ in range(12)],
    )
    assert Counter(r.strip() for r in results) == {"stress wake": 1, "-": 11}
    assert not wake_path().exists()
    assert not list(wake_path().parent.glob("*.claimed"))


@pytest.mark.slow
def test_killed_writer_releases_lock_without_losing_published_script():
    vthread.seed("task", ["确认"])
    before = vthread.thread_path().read_bytes()
    ready = paths.debug_dir() / "writer-ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import sys
from physiclaw.common.file_lock import exclusive_file_lock
from physiclaw.debug.thread import thread_path
with exclusive_file_lock(thread_path().with_suffix('.lock')):
    thread_path().with_name('writer-ready').touch()
    sys.stdin.read()
""",
        ],
        env={**os.environ, "PHYSICLAW_HOME": str(paths.HOME)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists(), "writer did not acquire the lock"
        child.kill()
        child.wait(timeout=5)
        assert vthread.thread_path().read_bytes() == before
        vthread.stage(["取消"])
        assert vthread.load().staged == ["确认", "取消"]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
