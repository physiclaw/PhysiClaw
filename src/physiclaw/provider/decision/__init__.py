"""Bounded-choice service interface, independent of chat history."""

from physiclaw.contract.decision import DecisionProvider
from physiclaw.provider.decision.decision_compat import BaseDecisionProvider

__all__ = ["BaseDecisionProvider", "DecisionProvider"]
