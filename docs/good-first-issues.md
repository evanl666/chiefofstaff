# Good first issues

Each of these is a self-contained change with a clear test. Comment on the matching GitHub
issue (or open one) before you start, so two people do not do the same one.

1. **Teams as an event source.** A `sources/teams.py` that finds new chats and mentions through
   a Microsoft Graph or Teams MCP server, like `sources/slack.py`. See
   [CONTRIBUTING.md](../CONTRIBUTING.md#add-an-event-source). Test with a fake client.
2. **Linear or Jira issues assigned to you** become events for the Router. Same pattern; the
   Router prompt may need one line on what to do with them.
3. **Recognize more test and build commands** in `receipts.py` (Nx, Pants, Turborepo, Buck,
   `just test`). Add cases to `tests/test_receipts.py`.
4. **Keyboard shortcuts** in the inbox: `j`/`k` to move between cards, `a` to accept, `f` to
   follow up, `?` for help. Plain JavaScript in `static/inbox.js` (cards) and `static/app.js`
   (wiring); add a step to `scripts/demo_smoke.py`.
5. **A weekly recap card**: what finished, what it cost, which workers were reused. Builds on
   `digest.py`; test like `test_daily_digest_once_a_day_and_only_when_useful`.
6. **Approve from your phone**: send cards that need you to a Slack DM to yourself (or ntfy)
   with a link back to the card. Start with a setting and a test with the fake Slack MCP.
7. **Show a task's runs** on its card (when there were several): when each started, why
   (start, follow-up, retry) and how it ended. Data is in `runs`.
8. **Better change stats for shared project folders** (`isolate=false`): count files the
   worker wrote (`sessions.files`) when it did not commit. `workspaces.changes`.
9. **Export a session's timeline** as markdown from its header, like *Copy report* for tasks.
10. **Translate the UI text** into another language behind a setting, starting with the inbox.
