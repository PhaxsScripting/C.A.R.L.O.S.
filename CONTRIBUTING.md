# Working on Carlos and HoloHand

Use this repository as the source of truth for released code. Keep each fix small
enough to review, run the relevant tests, and push it as soon as that change passes.
Do not leave finished changes waiting for the end of a long session.

Commit messages should sound like Phax: casual and specific. Examples:
`stop the cursor from teleporting lol` or `camera timestamps were cooked`.
Keep the message useful enough to find the fix later.

Keep credentials, local settings, captures, model weights, and private machine
rollback records out of Git. Run `python3 tools/check-release.py` after staging.
Never claim a synthetic hand test proves real-hand quality; report both separately.
