# Superseded prompt (D31)

`vision_extract_frozen_v1.md` is the screenshot prompt that produced the cached responses behind the current `output.csv`.
It listed ids printed in the dataset's screenshots as examples, which is file-specific text, so it was replaced by
`code/prompts/vision_extract.md` (id formats described as patterns only).

It is kept byte for byte only because the cached responses are keyed by its exact text. `code/tools/refresh_vision.py`
re-extracts the screenshots with the new prompt, repoints `code/config/frozen_v1.toml`, and then deletes this folder.
