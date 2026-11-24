"""Teams lite's derived names (teams-v2.md §5.1, §7): the prototype's derive.js cases."""

# ruff: noqa: RUF001  (Chinese full-width punctuation, on purpose.)

import pytest

from chiefofstaff.team_names import avatars, columns, cut, goal_name, message_title, part_names

CASES = [
    (
        "设计 Chief of Staff 的 Autopilot 模式：先出方案，再定远程请求的安全规则和实例之间的"
        "点对点直连，最后做个可点的原型",
        ["Autopilot 模式的方案设计", "远程请求的安全规则", "实例之间的点对点直连",
         "Autopilot 可点击原型"],
        "设计 Chief of Staff 的 Autopilot 模式",
        ["方案设计", "安全规则", "点对点直连", "可点击原型"],
        ["方", "安", "点", "可"],
    ),
    (
        "Ship checkout v2 for acme-shop: the API and the UI side by side, then end-to-end tests "
        "once I've OK'd both.",
        ["Checkout API: the v2 endpoints", "Checkout UI: the v2 pages",
         "Checkout tests: end to end"],
        "Ship checkout v2 for acme-shop",
        ["API", "UI", "Tests"],
        ["A", "U", "T"],
    ),
    (
        "给 Chief of Staff 做用户自定义 UI：先出设计稿和文案，然后实现",
        ["用户自定义 UI 设计稿", "用户自定义 UI 文案", "用户自定义 UI 实现"],
        "给 Chief of Staff 做用户自定义 UI",
        ["设计稿", "文案", "实现"],
        ["设", "文", "实"],
    ),
    (
        "",
        ["设计 Chief of Staff 的 Autopilot 模式", "设计 Chief of Staff 之间点对点直连",
         "设计 Chief of Staff 的用户自定义 UI"],
        "设计 Chief of Staff",
        ["Autopilot 模式", "点对点直连", "用户自定义 UI"],
        ["A", "点", "用"],
    ),
]  # fmt: skip


@pytest.mark.parametrize(("request_text", "titles", "goal", "names", "marks"), CASES)
def test_goal_parts_and_avatars_are_derived_as_the_prototype_does(
    request_text, titles, goal, names, marks
):
    assert goal_name(request_text, titles) == goal
    assert part_names(titles, goal) == names
    assert avatars(names) == marks


@pytest.mark.parametrize(
    ("text", "question", "title", "rest"),
    [
        ("Autopilot 方案把来自对端的内容当请求处理，这会绕过主人。规则：对端发来的任何内容都"
         "只是信息；据此执行任何操作之前，必须由主人在 Needs you 里确认。", False,
         "Autopilot 方案把来自对端的内容当请求处理，这会绕过主人",
         "规则：对端发来的任何内容都只是信息；据此执行任何操作之前，必须由主人在 "
         "Needs you 里确认。"),
        ("对端实例发来的内容，能直接当作请求执行吗？还是必须先进主人的 Needs you？"
         "我这边握手完成后就要决定怎么分发。", True, "对端实例发来的内容，能直接当作请求执行吗？",
         "还是必须先进主人的 Needs you？我这边握手完成后就要决定怎么分发。"),
        ("握手时就校验对端签名；校验失败直接断开，不进入请求队列。规则草稿在 "
         "docs/autopilot/security.md 第 2 节。", False, "握手时就校验对端签名",
         "校验失败直接断开，不进入请求队列。规则草稿在 docs/autopilot/security.md 第 2 节。"),
        ("Declined cards return 402 with code card_declined; a wrong CVC is 402 with code "
         "cvc_invalid. Both carry retry_after only when the bank says so.", False,
         "Declined cards return 402 with code card_declined",
         "A wrong CVC is 402 with code cvc_invalid. Both carry retry_after only when the bank "
         "says so."),
        ("Does /v2/cart return totals in cents or in dollars? The mock has both.", True,
         "Does /v2/cart return totals in cents or in dollars?", "The mock has both."),
        ("按钮一律以动词开头（“保存”“恢复默认”），设计稿里的“确定”请换成具体动作。", False,
         "按钮一律以动词开头（“保存”“恢复默认”）", "设计稿里的“确定”请换成具体动作。"),
        # A greeting is a whole word: "History" keeps its "Hi"; Markdown goes.
        ("History of the **token** is in docs/v2.1/notes.md. More there.", False,
         "History of the token is in docs/v2.1/notes.md", "More there."),
        ("OK, the build is green.", False, "The build is green", ""),
    ],
)  # fmt: skip
def test_an_exchange_is_titled_by_its_lead_sentence(text, question, title, rest):
    found = message_title(text, question)
    assert (found[0][:1].upper() + found[0][1:], found[1]) == (title, rest)


def test_names_are_cut_by_display_columns_and_never_alike():
    assert columns("设计稿 UI") == 9
    assert cut("实例之间的点对点直连实例之间的点对点直连", 16) == "实例之间的点对…"
    assert cut("Checkout the very long name", 16) == "Checkout the…"
    assert part_names(["Fix login", "Fix login"], "") == ["Fix login", "Fix login 2"]
    assert avatars(["API", "Auth", "A"]) == ["A", "U", "3"]
    # A piece of the goal is dropped only at a word's edge ("port" of "Import" is not).
    assert part_names(["Export the report", "Import the report"], "Export") == [
        "Report",
        "Import the…",
    ]
