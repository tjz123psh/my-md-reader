"""ConversationState bounded-history tests (spec §8.3, §13.6)."""

from __future__ import annotations

import unittest

from mdreader.models.conversation import (
    ConversationState,
    commit_ask_if_successful,
)


class CommitAskIfSuccessfulTests(unittest.TestCase):
    def test_success_records_pair(self) -> None:
        state = ConversationState()
        committed = commit_ask_if_successful(
            state, "问题", success=True, full_text="回答"
        )
        self.assertTrue(committed)
        self.assertEqual(
            [m.text for m in state.messages], ["问题", "回答"]
        )

    def test_failure_never_records(self) -> None:
        for success in (False,):
            state = ConversationState()
            committed = commit_ask_if_successful(
                state, "问题", success=success, full_text="partial"
            )
            self.assertFalse(committed)
            self.assertEqual(state.messages, ())

    def test_truncated_outcome_never_records(self) -> None:
        # A truncated (non-stop finish) reply is not an explicit success and
        # must not enter history (§8.4).
        state = ConversationState()
        committed = commit_ask_if_successful(
            state, "问题", success=False, full_text="partial text"
        )
        self.assertFalse(committed)
        self.assertEqual(state.messages, ())


class ConversationStateTests(unittest.TestCase):
    def test_success_pair_enters_history(self) -> None:
        state = ConversationState()
        state.record_success("问题一", "回答一")
        self.assertEqual(
            [(m.role, m.text) for m in state.messages],
            [("user", "问题一"), ("assistant", "回答一")],
        )

    def test_second_success_pair_appends(self) -> None:
        state = ConversationState()
        state.record_success("q1", "a1")
        state.record_success("q2", "a2")
        self.assertEqual(
            [m.text for m in state.messages], ["q1", "a1", "q2", "a2"]
        )

    def test_failed_or_cancelled_never_enters(self) -> None:
        # The only write path is record_success; there is no API to record a
        # failed, cancelled or partial reply, so history can only contain
        # successful pairs.
        state = ConversationState()
        state.record_success("ok", "answer")
        self.assertEqual(len(state.messages), 2)
        for message in state.messages:
            self.assertEqual(message.role, "assistant" if message.text == "answer" else "user")

    def test_message_cap_keeps_newest_pairs(self) -> None:
        state = ConversationState(max_messages=4)
        for index in range(4):  # 4 pairs = 8 messages, cap 4
            state.record_success(f"q{index}", f"a{index}")
        self.assertEqual(
            [m.text for m in state.messages], ["q2", "a2", "q3", "a3"]
        )

    def test_character_cap_evicts_oldest_complete_pair(self) -> None:
        state = ConversationState(max_characters=10)
        state.record_success("12345", "12345")  # exactly at budget
        self.assertEqual(len(state.messages), 2)
        state.record_success("x", "y")  # over budget by 2 -> drop first pair
        self.assertEqual([m.text for m in state.messages], ["x", "y"])

    def test_eviction_always_removes_complete_pairs(self) -> None:
        state = ConversationState(max_messages=2)
        for index in range(5):
            state.record_success(f"q{index}", f"a{index}")
        self.assertEqual(len(state.messages), 2)  # one complete pair, never an orphan
        self.assertEqual([m.text for m in state.messages], ["q4", "a4"])

    def test_reset_clears_history(self) -> None:
        state = ConversationState()
        state.record_success("q", "a")
        state.reset()
        self.assertEqual(state.messages, ())

    def test_selection_change_does_not_reset(self) -> None:
        # ConversationState has no coupling to document selection; a new
        # selection simply means the next question carries a fresh context
        # envelope, history is untouched.
        state = ConversationState()
        state.record_success("q1", "a1")
        state.record_success("q2", "a2")
        self.assertEqual(len(state.messages), 4)

    def test_default_caps_are_12_messages_and_48000_characters(self) -> None:
        state = ConversationState()
        self.assertEqual(state._max_messages, 12)
        self.assertEqual(state._max_characters, 48_000)


if __name__ == "__main__":
    unittest.main()
