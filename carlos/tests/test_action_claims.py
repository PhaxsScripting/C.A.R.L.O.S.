import unittest
from ev.action_claims import claims_computer_action


class ActionClaimTests(unittest.TestCase):
    def test_unexecuted_promises_and_progress_captions_are_flagged(self):
        for text in (
            "Okay, I'll unpause your Spotify music.",
            "Opening YouTube in Firefox...",
            "Sure! I've saved the file.",
            "I'm now moving the window.",
            "Done!",
            "I resumed Spotify.",
            "I will click Open.",
            "Launching Firefox now.",
            "I'm going to open the YouTube website using your Firefox browser.",
        ):
            with self.subTest(text=text):
                self.assertTrue(claims_computer_action(text))

    def test_questions_explanations_and_conditional_offers_are_not_action_claims(self):
        for text in (
            "Opening files requires an application.",
            "You could make a mango smoothie.",
            "I can explain how to open Firefox.",
            "I'll open it if you ask me to.",
            "I can't open it because permission is missing.",
            "To pause Spotify, use the media controls.",
            'The label says "Opening Firefox...".',
        ):
            with self.subTest(text=text):
                self.assertFalse(claims_computer_action(text))
