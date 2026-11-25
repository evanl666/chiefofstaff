"""Teams lite's derived names (teams-v2.md §5.1, §7): the prototype's derive.js cases."""

# ruff: noqa: RUF001, E501  (Chinese full-width punctuation; the corpus's requests whole.)

import re

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
    # A piece of the goal is dropped only at a word's edge ("port" of "Import" is not), and
    # never leaving one word of a title ("Page" of "Login page").
    assert part_names(["Export the report", "Import the report"], "Export") == [
        "Export the report",
        "Import the report",
    ]
    assert part_names(["Export the sales report", "Import it"], "Export") == [
        "Sales report",
        "Import it",
    ]


# The team-quality corpus: made-up requests as owners write them, in Chinese and English, with
# their parts' titles as the Router gives them; each named as a person would name it.
CORPUS = [
    (
        "给 acme-shop 加上按单退款：后端出退款 API，订单页加退款按钮和确认弹窗，退款成功发邮件通知，最后补端到端测试。",
        ["退款 API：POST /v2/orders/{id}/refunds", "订单页：退款按钮和确认弹窗", "邮件通知：退款成功后发邮件", "端到端测试：退款的全流程"],
        "给 acme-shop 加上按单退款",
        ["API", "订单页", "邮件通知", "端到端测试"],
    ),
    (
        "Ship checkout v2 for acme-shop: the API and the UI side by side, then end-to-end tests.",
        ["Checkout API: the v2 endpoints", "Checkout UI: the v2 pages", "Checkout tests: end to end"],
        "Ship checkout v2 for acme-shop",
        ["API", "UI", "Tests"],
    ),
    (
        "Write the migration for the invoices table, then switch the billing API to it.",
        ["Invoices table migration", "Billing API on the invoices table"],
        "Write the migration for the invoices table",
        ["Migration", "Billing API"],
    ),
    (
        "Generate a token and write it into the config.",
        ["Token writer: put the token in config", "Token scout: generate a token"],
        "Generate a token and write it into the config",
        ["Token writer", "Token scout"],
    ),
    (
        "先写 Autopilot 的方案，方案好了再做个可点的原型。",
        ["Autopilot 方案", "Autopilot 可点击原型"],
        "写 Autopilot 的方案",
        ["方案", "可点击原型"],
    ),
    (
        "这周的三件事：先修登录页，再给登录页补截图测试；另外整理一下 README。",
        ["修复登录页", "登录页截图测试"],
        "这周的三件事",
        ["修复登录页", "登录页截图测试"],
    ),
    (
        "帮我把登录页改成新的设计稿，然后把注册页也改了，最后补上截图测试",
        ["登录页换新设计", "注册页换新设计", "登录和注册页截图测试"],
        "把登录页改成新的设计稿",
        ["登录页换新设计", "注册页换新设计", "登录和注册页截图测试"],
    ),
    (
        "Hey, so the nightly export has been failing since Tuesday and finance is blocked. Can you figure out why the export job dies at 10k rows, fix it, and then backfill the missing three days? Also add an alert so we hear about it next time.",
        ["Nightly export: find why it dies at 10k rows", "Backfill the missing export days", "Alert on export failures"],
        "Nightly export",
        ["Find why it dies", "Backfill missing days", "Alert on export failures"],
    ),
    (
        "这个报错帮我看下：\nTraceback (most recent call last):\n  File \"app.py\", line 12, in <module>\n    user = users[payload['user_id']]\nKeyError: 'user_id'\n然后修一下，再加个测试",
        ["修复 app.py 的 KeyError: user_id", "为缺少 user_id 的请求加测试"],
        "修复 app.py 的 KeyError",
        ["修复 app.py 的 KeyError", "请求加测试"],
    ),
    (
        "can you look into this? https://github.com/acme/web/issues/88 — fix it and add a regression test",
        ["Fix acme/web#88: date picker off by one", "Regression test for acme/web#88"],
        "Fix acme/web#88",
        ["Date picker off by one", "Regression test"],
    ),
    (
        "设计 Chief of Staff 的 Autopilot 模式：先出方案，再定远程请求的安全规则和实例之间的点对点直连，最后做个可点的原型",
        ["Autopilot 模式的方案设计", "远程请求的安全规则", "实例之间的点对点直连", "Autopilot 可点击原型"],
        "设计 Chief of Staff 的 Autopilot 模式",
        ["方案设计", "安全规则", "点对点直连", "可点击原型"],
    ),
    (
        "给 Chief of Staff 做用户自定义 UI：先出设计稿和文案，然后实现",
        ["用户自定义 UI 设计稿", "用户自定义 UI 文案", "用户自定义 UI 实现"],
        "给 Chief of Staff 做用户自定义 UI",
        ["设计稿", "文案", "实现"],
    ),
    (
        "Please migrate the reports service from Python 3.9 to 3.12, update the Dockerfile and CI, and make sure all the integration tests still pass afterwards.",
        ["Reports service on Python 3.12", "Dockerfile and CI on Python 3.12", "Integration tests on Python 3.12"],
        "Reports service on Python 3.12",
        ["Reports service", "Dockerfile and CI", "Integration tests"],
    ),
    (
        "麻烦你把支付页的金额显示统一成两位小数，同时把后台导出的金额也改一下",
        ["支付页金额两位小数", "后台导出金额两位小数"],
        "把支付页的金额显示统一成两位小数",
        ["支付页金额两位小数", "后台导出金额两位小数"],
    ),
    (
        "We need dark mode for the settings page and the dashboard; share the color tokens between them.",
        ["Dark mode: color tokens", "Dark mode: settings page", "Dark mode: dashboard"],
        "Dark mode",
        ["Color tokens", "Settings page", "Dashboard"],
    ),
    (
        "I want to add SSO login with external identity providers. Backend first (SAML + OIDC), then the login page button, then docs for admins.",
        ["SSO backend: SAML and OIDC", "Login page: SSO button", "Admin docs: setting up SSO"],
        "Add SSO login with external identity providers",
        ["SSO backend", "Login page", "Admin docs"],
    ),
    (
        "OK so: 1) bump the SDK to v5 2) fix whatever breaks in the mobile app 3) release a beta build",
        ["Bump the SDK to v5", "Fix the mobile app on SDK v5", "Mobile beta build on SDK v5"],
        "Bump the SDK to v5",
        ["Bump the SDK to v5", "Fix the mobile app", "Mobile beta build"],
    ),
    (
        "数据看板加一个按渠道拆分的转化率图表，后端先出聚合接口，前端再画图",
        ["转化率聚合接口（按渠道）", "数据看板：按渠道的转化率图表"],
        "数据看板加一个按渠道拆分的转化率图表",
        ["转化率聚合接口", "数据看板"],
    ),
    (
        "The onboarding emails are a mess. Rewrite the copy for all five, then wire them into the new scheduler.",
        ["Onboarding emails: new copy", "Onboarding emails on the new scheduler"],
        "Onboarding emails",
        ["New copy", "New scheduler"],
    ),
    (
        "把 acme-analytics 的埋点 SDK 从 v2 升到 v3，web 和 iOS 两端一起改，改完跑一遍回归",
        ["web 端埋点 SDK 升级到 v3", "iOS 端埋点 SDK 升级到 v3", "埋点 v3 回归测试"],
        "把 acme-analytics 的埋点 SDK 从 v2 升到 v3",
        ["Web 端埋点 SDK 升级到 v3", "iOS 端埋点 SDK 升级到 v3", "v3 回归测试"],
    ),
    (
        "Investigate why p95 latency on the search API doubled last week and propose a fix; separately someone should check if the cache hit rate dropped.",
        ["Search API p95 latency doubled: why", "Search cache hit rate last week"],
        "Search API p95 latency doubled",
        ["API p95 latency doubled", "Cache hit rate last week"],
    ),
    (
        "帮我整理一下：先把 docs/ 下面过时的页面列出来，再逐个更新，最后把侧边栏的目录也调好",
        ["列出 docs/ 下过时的页面", "更新过时的文档页面", "调整文档侧边栏目录"],
        "把 docs/ 下面过时的页面列出来",
        ["列出 docs/ 下过时的页面", "更新过时的文档页面", "调整文档侧边栏目录"],
    ),
    (
        "Refactor the internationalization layer so translators can work without engineers: extract every string, set up the translation files, and add a CI check that fails on missing keys",
        ["Extract UI strings for translation", "Translation files and loader", "CI check for missing translation keys"],
        "Refactor the internationalization layer",
        ["Extract UI strings", "Translation files", "CI check"],
    ),
    (
        "我们要给 acme-crm 加一个批量导入客户的功能，Excel 和 CSV 都要支持，导入前要能预览和校验，导入后发一封汇总邮件给操作人。",
        ["批量导入：解析 Excel 和 CSV", "批量导入：预览和校验", "批量导入：导入后发汇总邮件"],
        "给 acme-crm 加一个批量导入客户的功能",
        ["解析 Excel 和 CSV", "预览和校验", "导入后发汇总邮件"],
    ),
]  # fmt: skip


@pytest.mark.parametrize(("request_text", "titles", "goal", "names"), CORPUS)
def test_the_corpus_is_named_as_a_person_would(request_text, titles, goal, names):
    assert goal_name(request_text, titles) == goal
    assert part_names(titles, goal) == names


@pytest.mark.parametrize(("request_text", "titles", "goal", "names"), CORPUS)
def test_a_name_is_short_whole_and_distinct(request_text, titles, goal, names):
    found = goal_name(request_text, titles)
    parts = part_names(titles, found)
    assert 4 <= columns(found) <= 48
    assert found != request_text.strip()
    assert all(2 <= columns(name) <= 24 for name in parts)
    assert len(set(parts)) == len(parts)
    for name in [found, *parts]:
        assert not name.endswith("\u2026")
        assert not re.search(
            r"\s(?:the|a|an|of|for|to|in|on|at|with|and|or|by|from)$|[的和与]$", name
        )
        assert not re.match(r"(?i)(?:please|can you|hey|ok|帮我|麻烦|请你|我们要)", name)
        # Its last word is one of the owner's or the Router's, whole ("days", not "da").
        last = re.findall(r"[A-Za-z0-9_.#/-]+$", name)
        if last:
            said = f"{request_text} {' '.join(titles)}"
            assert re.search(rf"(?<![\w]){re.escape(last[0])}(?![\w])", said, re.IGNORECASE)
