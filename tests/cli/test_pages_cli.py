"""Public page commands, with real packs/recordings and a fake MCP boundary.

Mutation review accepts help/diagnostic decorations and display formatting,
a non-skip prompt default (still captures), and None in place of the empty
guided-negative list (capture treats both as no negative observations).
"""

import asyncio
import json

import pytest
from mcp import MCPError
from typer.testing import CliRunner

from physiclaw.cli import pages
from physiclaw.common import paths
from physiclaw.common.listing import LISTING_HEADER
from physiclaw.conductor.bench import corpus
from physiclaw.conductor.load import prints

runner = CliRunner()
LISTING = f'{LISTING_HEADER}\n0 [text] "综合" [0.1,0.1,0.2,0.12] 0.90'
SID = "20261015-000000-abcdef"


def test_no_command_displays_available_commands():
    result = runner.invoke(pages.pages_app, [])

    assert result.exit_code == 2
    assert all(
        name in result.output for name in ("extract", "match", "calibrate", "propose")
    )


@pytest.fixture
def pack():
    root = paths.playbooks_dir() / "sample"
    root.mkdir(parents=True)
    manifest = root / paths.PACK_FILENAME
    manifest.write_text(
        "schema: 1\nkind: manifest\napp: sample\npages:\n"
        "  home:\n    description: Home screen\n    anchors: ['综合']\n",
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def listing_file(tmp_path):
    path = tmp_path / "listing.txt"
    path.write_text(LISTING, encoding="utf-8")
    return path


@pytest.fixture
def recording():
    root = paths.sessions_dir() / SID
    root.mkdir(parents=True)
    event = {"event": "tool_result", "text": LISTING}
    (root / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    return root


@pytest.fixture
def labeled_corpus(tmp_path):
    path = tmp_path / "labeled.jsonl"
    corpus.write_corpus(path, [corpus.CorpusItem("sample.home", LISTING)])
    return path


class PeekClient:
    def __init__(self):
        self.events = []
        self.replies = iter([[{"type": "image"}, {"type": "text", "text": LISTING}]])
        self.error = None
        self.connect_error = None

    async def __aenter__(self):
        self.events.append("enter")
        if self.connect_error is not None:
            raise self.connect_error
        return self

    async def __aexit__(self, *args):
        self.events.append("close")

    async def call_tool(self, name, arguments):
        self.events.append((name, arguments))
        if self.error is not None:
            raise self.error
        return next(self.replies)


@pytest.fixture
def client(mocker):
    client = PeekClient()
    mocker.patch("physiclaw.agent.engine.mcp_tool.McpClient", return_value=client)
    return client


@pytest.mark.parametrize("command", [["propose"], ["match", "sample"]])
@pytest.mark.parametrize(
    "inputs",
    [
        [],
        ["--live", "--listing", "missing.txt"],
        ["--session", "missing", "--listing", "missing.txt"],
        ["--live", "--session", "missing"],
        ["--live", "--session", "missing", "--listing", "missing.txt"],
    ],
)
def test_regression_input_sources_must_be_exclusive_before_peek(
    pack, client, command, inputs
):
    result = runner.invoke(pages.pages_app, command + inputs)

    assert result.exit_code == 2
    assert "exactly one of --session / --listing / --live" in result.output
    assert client.events == []


@pytest.mark.parametrize("command", [["propose"], ["match", "sample"]])
def test_regression_missing_listing_reports_path(pack, tmp_path, command):
    missing = tmp_path / "missing.txt"

    result = runner.invoke(pages.pages_app, command + ["--listing", str(missing)])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and str(missing) in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    "command, expected",
    [
        (["propose"], "candidates: '综合'"),
        (["match", "sample"], "match sample.home (1 anchor)"),
    ],
)
def test_listing_is_parsed_by_public_command(pack, listing_file, command, expected):
    result = runner.invoke(pages.pages_app, command + ["--listing", str(listing_file)])

    assert result.exit_code == 0
    assert "[  0]" in result.stdout
    assert expected in result.stdout


def test_session_suffix_proposes_recorded_anchors(recording):
    result = runner.invoke(pages.pages_app, ["propose", "--session", "abcdef"])

    assert result.exit_code == 0
    assert "candidates: '综合'" in result.stdout


@pytest.mark.parametrize(
    "text, kind, page_id",
    [
        (LISTING, "match", "sample.home"),
        (LISTING.replace("综合", "结算"), "unknown", "-"),
    ],
)
def test_match_columns_identify_known_and_unknown_screens(
    pack, listing_file, text, kind, page_id
):
    listing_file.write_text(text, encoding="utf-8")

    result = runner.invoke(
        pages.pages_app, ["match", "sample", "--listing", str(listing_file)]
    )

    assert result.exit_code == 0
    assert result.stdout.split("] ", 1)[1].split()[:3] == [kind, page_id, "dy=+0.00"]


def test_live_propose_closes_client_and_uses_screen_not_action(client):
    client.replies = iter(
        [
            [
                {"type": "text", "text": "action echo"},
                {"type": "image"},
                {"type": "text", "text": LISTING},
            ]
        ]
    )

    result = runner.invoke(pages.pages_app, ["propose", "--live"])

    assert result.exit_code == 0
    assert "candidates: '综合'" in result.stdout
    assert client.events == ["enter", ("peek", {}), "close"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("peek failed"),
        OSError("transport down"),
        MCPError(-32000, "protocol failed"),
    ],
)
def test_regression_live_failure_reports_cause_and_closes_client(client, error):
    client.error = error

    result = runner.invoke(pages.pages_app, ["propose", "--live"])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and str(error) in result.output
    assert client.events == ["enter", ("peek", {}), "close"]
    assert isinstance(result.exception, SystemExit)


def test_live_connect_failure_prints_start_hint(client):
    client.connect_error = ConnectionError("server unavailable")

    result = runner.invoke(pages.pages_app, ["propose", "--live"])

    assert result.exit_code == 1
    assert "server unavailable" in result.output
    assert "physiclaw mcp" in result.output
    assert client.events == ["enter"]


@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(7)]
)
def test_live_control_flow_exceptions_propagate_after_cleanup(client, error):
    client.error = error

    with pytest.raises(type(error)) as caught:
        pages.propose(session=None, listing=None, live=True)

    assert caught.value is error
    assert client.events == ["enter", ("peek", {}), "close"]


def test_extract_writes_unlabeled_recordings(recording, tmp_path):
    output = tmp_path / "corpus.jsonl"

    result = runner.invoke(pages.pages_app, ["extract", "abcdef", "--out", str(output)])

    assert result.exit_code == 0
    assert "wrote 1 listings" in result.stdout
    assert corpus.read_corpus(output) == [corpus.CorpusItem("?", LISTING)]


def test_regression_extract_write_failure_reports_destination(recording, tmp_path):
    output = tmp_path / "missing" / "corpus.jsonl"

    result = runner.invoke(pages.pages_app, ["extract", "abcdef", "--out", str(output)])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and str(output) in result.output
    assert "wrote" not in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        ["propose", "--session", "abcdef"],
        ["extract", "abcdef", "--out", "unused.jsonl"],
    ],
)
def test_regression_recording_without_logs_reports_error(recording, command):
    (recording / "events.jsonl").unlink()

    result = runner.invoke(pages.pages_app, command)

    assert result.exit_code == 1
    assert (
        "ERROR [" in result.stderr and "no events.jsonl or model.jsonl" in result.output
    )


@pytest.mark.parametrize(
    "command", [["match", "sample", "--live"], ["calibrate", "sample", "--guided"]]
)
def test_regression_invalid_pack_is_reported_before_live_access(pack, client, command):
    pack.write_text(
        "schema: 1\nkind: manifest\napp: sample\npages: {home: {anchors: []}}",
        encoding="utf-8",
    )

    result = runner.invoke(pages.pages_app, command)

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and "anchors" in result.output
    assert client.events == []


@pytest.mark.parametrize(
    "command", [["match", "absent", "--live"], ["calibrate", "absent", "--guided"]]
)
def test_undeclared_app_is_reported_before_live_access(client, command):
    result = runner.invoke(pages.pages_app, command)

    assert result.exit_code == 1
    assert "no pages declared" in result.output
    assert client.events == []


@pytest.mark.parametrize("shots", ["0", "-1"])
def test_regression_nonpositive_shots_fail_before_prompt(pack, client, shots):
    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", "--guided", "--shots", shots]
    )

    assert result.exit_code == 2
    assert "put the phone" not in result.output
    assert client.events == []


@pytest.mark.parametrize(
    "args, diagnostic",
    [
        ([], "need a corpus file"),
        (["file.jsonl", "--guided"], "drop the corpus argument"),
    ],
)
def test_calibration_requires_exactly_one_mode(pack, client, args, diagnostic):
    result = runner.invoke(pages.pages_app, ["calibrate", "sample"] + args)

    assert result.exit_code == 2
    assert diagnostic in result.output
    assert client.events == []


def test_calibrate_saves_learned_geometry_and_reports_pass_rate(pack, labeled_corpus):
    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", str(labeled_corpus)]
    )

    assert result.exit_code == 0
    assert (
        "sample.home: obs=1 anchors=1 genuine_pass=100% impostors_read=-"
        in result.stdout
    )
    assert "saved learned/pages/sample.json (1 pages)" in result.stdout
    learned = prints.load_learned("sample")["home"]
    assert learned.observations == 1
    assert learned.anchors["综合"].cx == 0.15


def test_calibrate_warns_when_negatives_also_match(pack, labeled_corpus):
    corpus.write_corpus(
        labeled_corpus,
        [
            corpus.CorpusItem("sample.home", LISTING),
            corpus.CorpusItem("other", LISTING),
        ],
    )

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", str(labeled_corpus)]
    )

    assert result.exit_code == 0
    assert "impostors_read=1" in result.stdout and "INSEPARABLE" in result.stdout


def test_calibrate_unlabeled_corpus_warns_without_saving(pack, labeled_corpus):
    corpus.write_corpus(labeled_corpus, [corpus.CorpusItem("?", LISTING)])

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", str(labeled_corpus)]
    )

    assert result.exit_code == 0
    assert "no observations — skipped" in result.stdout
    assert not prints.learned_file("sample").exists()


@pytest.mark.parametrize(
    "content", ["not JSON", "[]", '{"label": null, "listing": 42}']
)
def test_regression_bad_corpus_reports_file_and_line(pack, labeled_corpus, content):
    labeled_corpus.write_text(content, encoding="utf-8")

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", str(labeled_corpus)]
    )

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and f"{labeled_corpus}:1:" in result.output
    assert not prints.learned_file("sample").exists()


def test_regression_missing_corpus_reports_path(pack, tmp_path):
    path = tmp_path / "missing.jsonl"

    result = runner.invoke(pages.pages_app, ["calibrate", "sample", str(path)])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and str(path) in result.output


def test_regression_failed_learned_save_is_reported(pack, labeled_corpus):
    paths.learned_pages_dir().mkdir(parents=True)
    prints.learned_file("sample").mkdir()

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", str(labeled_corpus)]
    )

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr and "sample.json" in result.output
    assert "saved learned" not in result.stdout


def test_guided_capture_skips_unreadable_peeks_and_saves_readable(pack, client):
    client.replies = iter([[], [{"type": "image"}, {"type": "text", "text": LISTING}]])

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", "--guided", "--shots", "2"], input="\n"
    )

    assert result.exit_code == 0
    assert "[1/2] unreadable view" in result.stdout
    assert "[2/2] 1 rows" in result.stdout
    assert prints.load_learned("sample")["home"].observations == 1
    assert client.events == ["enter", ("peek", {}), "close"] * 2


@pytest.mark.parametrize("answer", ["s", " S "])
def test_guided_skip_does_not_peek_or_save(pack, client, answer):
    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", "--guided"], input=answer + "\n"
    )

    assert result.exit_code == 1
    assert "skipped" in result.stdout and "no pages captured" in result.output
    assert client.events == []
    assert not prints.learned_file("sample").exists()


def test_guided_unreadable_page_is_not_saved(pack, client):
    client.replies = iter([[]])

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", "--guided", "--shots", "1"], input="\n"
    )

    assert result.exit_code == 1
    assert "no pages captured" in result.output
    assert not prints.learned_file("sample").exists()


def test_guided_default_captures_four_observations(pack, client):
    client.replies = iter([[{"type": "image"}, {"type": "text", "text": LISTING}]] * 4)

    result = runner.invoke(
        pages.pages_app, ["calibrate", "sample", "--guided"], input="\n"
    )

    assert result.exit_code == 0
    assert prints.load_learned("sample")["home"].observations == 4
    assert client.events == ["enter", ("peek", {}), "close"] * 4


@pytest.mark.parametrize("answers, kept", [("\ns\n", "alpha"), ("s\n\n", "zulu")])
def test_guided_pages_are_prompted_in_name_order_and_can_be_partly_skipped(
    pack, client, answers, kept
):
    pack.write_text(
        "schema: 1\nkind: manifest\npages:\n"
        "  zulu: {description: Last, anchors: ['综合']}\n"
        "  alpha: {description: First, anchors: ['综合']}\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        pages.pages_app,
        ["calibrate", "sample", "--guided", "--shots", "1"],
        input=answers,
    )

    assert result.exit_code == 0
    assert result.stdout.index("sample.alpha") < result.stdout.index("sample.zulu")
    assert set(prints.load_learned("sample")) == {kept}
    assert client.events == ["enter", ("peek", {}), "close"]
