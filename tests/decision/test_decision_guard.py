"""Consequential labels veto both gestures without granting safety otherwise."""

from dataclasses import replace

import pytest

from physiclaw.contract.decision import DecisionContext, DecisionElement
from physiclaw.contract.observation import read_observation
from physiclaw.decision.guard import blocked_controls
from physiclaw.decision.policy import question_profile


@pytest.fixture
def control(observation_blocks):
    frame = read_observation(observation_blocks()).frame

    def make(text, *, label_box=None):
        target = DecisionElement(f"{frame.id}:0", (0.4, 0.4, 0.6, 0.5))
        label = DecisionElement(f"{frame.id}:1", label_box or target.bbox, text)
        context = DecisionContext(frame.id, frame=frame, ocr=(label,), icons=(target,))
        return context, target, label

    return make


@pytest.mark.parametrize(
    "text",
    [
        "发送",
        "发 送",
        "发\u200b送",
        "Send",
        "ＳＥＮＤ",
        "se\u200bnd",
        "S E N D",
        "发送给朋友",
        "立即支付 ¥99.00",
        "付款",
        "确认订单",
        "提交订单",
        "立即购买",
        "删除",
        "清空",
        "授权",
        "允许",
        "同意并继续",
        "取消订单",
        "按住说话",
        "录音",
        "PAY NOW",
        "Buy again",
        "Place order",
        "Add to cart",
        "Delete",
        "Allow",
        "Confirm",
        "Submit",
        "Record audio",
        "Hold to talk",
        "Sign in",
        "Sign in with Google",
        "Place an order",
        "Hold to talk now",
        "Log out",
        "Upload",
        "Share",
        "Call",
    ],
)
def test_known_consequential_labels_return_actual_observed_evidence(control, text):
    context, target, label = control(text)
    assert blocked_controls(context)[target.ref] is label


@pytest.mark.parametrize(
    "text", ["搜索", "微信", "淘宝", "Search", "Help", "Sendai", "PayPal"]
)
def test_normal_labels_and_english_word_substrings_do_not_create_false_matches(
    control, text
):
    context, target, _ = control(text)
    # This is absence of keyword evidence, not an assertion that the action is safe.
    assert target.ref not in blocked_controls(context)


def test_unlabelled_icon_is_vetoed_by_nearby_ocr(control):
    context, target, label = control("支付", label_box=(0.42, 0.515, 0.58, 0.535))
    assert target.text is None
    assert blocked_controls(context)[target.ref] is label


def test_unrelated_payment_label_elsewhere_does_not_block_a_search_target(control):
    context, target, _ = control("支付", label_box=(0.8, 0.8, 0.95, 0.9))
    assert target.ref not in blocked_controls(context)


def test_target_text_is_checked_even_when_it_arrives_in_icon_channel(control):
    context, target, _ = control(None)
    labelled_target = replace(target, text="Send")
    context = replace(context, icons=(labelled_target,))
    assert blocked_controls(context)[target.ref] is labelled_target


def test_batch_vetoes_keep_first_matching_evidence_for_each_observed_target(control):
    context, target, label = control("支付")
    send = DecisionElement(f"{context.observation_id}:2", target.bbox, "发送")
    search = DecisionElement(
        f"{context.observation_id}:3", (0.1, 0.1, 0.2, 0.2), "搜索"
    )
    context = replace(context, ocr=(search, label, send))
    blocked = blocked_controls(context)
    assert set(blocked) == {target.ref, label.ref, send.ref}
    assert all(evidence is label for evidence in blocked.values())


@pytest.mark.parametrize("tool", ["tap", "long_press"])
def test_all_blocked_targets_produce_no_questions_and_keep_veto_evidence(control, tool):
    context, target, label = control("发送")
    profile = question_profile(context, tools={tool}).to_dict()
    assert profile["questions"] == {}
    assert profile["blocked_targets"] == {
        ref: {
            "reason": "consequential_control_label",
            "evidence_ref": label.ref,
            "text": "发送",
        }
        for ref in (target.ref, label.ref)
    }


def test_blocked_target_is_absent_while_all_other_targets_remain(
    control,
):
    context, target, label = control("支付")
    search = DecisionElement(
        f"{context.observation_id}:2", (0.1, 0.1, 0.25, 0.2), "搜索"
    )
    context = replace(context, ocr=(*context.ocr, search))
    profile = question_profile(context, tools={"tap", "long_press"}).to_dict()
    assert set(profile["questions"]) == {"phase", "action_type", "target"}
    assert set(profile["questions"]["target"]["options"]) == {"NONE", "b2"}
    assert profile["questions"]["target"]["options"]["b2"]["target_ref"] == search.ref
    assert profile["blocked_targets"][target.ref]["evidence_ref"] == label.ref
