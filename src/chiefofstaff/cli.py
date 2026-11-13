"""The `chiefofstaff` command: `chiefofstaff send …` sends to the running Chief of Staff
(send.py, standard library only, so it starts at once); `chiefofstaff tidy-sessions` moves the
Copilot sessions a worker or a script made out of the way, with --apply (tidy.py); any other
arguments run Chief of Staff as they always did (__main__.py)."""

import sys


def main():
    if sys.argv[1:2] == ["send"]:
        from .send import main as send

        return send(sys.argv[2:])
    if sys.argv[1:2] == ["tidy-sessions"]:
        from .tidy import main as tidy

        return tidy(sys.argv[2:])
    from .__main__ import main as run

    return run()
