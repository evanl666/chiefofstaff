"""QA round 14 on Home's Artifacts (E58, E71, E73, E76, E77): one word for a run's status in its
row, in its runs line and in "+N"; a run's time since it stood so; what needs a look of any
task above the merged PRs of another; documents, runs and CI builds known wherever they are
served."""

import json

from test_round9_ui import DOM
from test_round13_ui import AGO, ARTIFACTS, NINE
from test_ui_text import run_js


def test_one_word_for_a_runs_status_on_its_row_its_line_and_plus_n():
    """E76: queued, waiting and running read "3 running" on their line and "pending, pending,
    running" under it; "+N" said its workers' own words; a line of runs all done said nothing."""
    found = run_js(
        DOM
        + ARTIFACTS
        + AGO
        + "const run = (i, status) => ({ kind: 'flow', url: `https://flyte.x/r/${i}`,"
        " title: `Run ${i}`, status, at: at(i + 1) });\n"
        "const task = (id, ...items) => ({ id, title: `Task ${id}`, status: 'done',"
        " session_id: 'w', updated_at: at(0), deliverables: items });\n"
        "s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [],"
        " artifacts_hidden: [], pr_titles: {} };\n"
        "const mixed = task('a', run(0, 'running'), run(1, 'queued'), run(2, 'waiting'));\n"
        "const shipped = task('b', ...['delivered', 'finished (succeeded)', 'succeeded', 'done']"
        ".map((status, i) => run(10 + i, status)));\n"
        "const groups = a.groupArtifacts(a.collectArtifacts([mixed, shipped]));\n"
        "const a1 = groups.find((g) => g.task.id === 'a');\n"
        "const b1 = groups.find((g) => g.task.id === 'b');\n"
        "const { rows } = a.artifactItems([mixed], () => {});\n"
        "const header = walk(rows[0].build()).find((n) => n.className === 'a-status');\n"
        "const open = a.groupArtifacts(a.collectArtifacts([mixed]), new Set(['a']))[0];\n"
        "return { a: [a1.folded.words, a1.folded.hiddenWords, header.textContent],"
        " rows: open.runs.map(a.shortStatus),"
        " b: [b1.folded.words, b1.folded.tone, b1.folded.hiddenWords] };"
    )
    assert found["a"] == ["1 running · 2 pending", "2 pending", "1 running · 2 pending"]
    assert found["rows"] == ["running", "pending", "pending"]
    assert found["b"] == ["4 done", "good", "3 done"]


def test_a_runs_time_is_since_it_stood_so():
    """E77: "ended · 3d ago" for a run just marked ended (its worker's first word, 3 days ago)."""
    found = run_js(
        ARTIFACTS + AGO + "const url = 'https://flyte.x/r/1';\n"
        "const task = { id: 'a', title: 'A', status: 'done', updated_at: at(0),"
        " artifacts: [{ url, at: at(72) }], deliverables: [{ kind: 'flow', url, title: 'Run',"
        " status: 'ended', said_by: 'you', changed_at: at(0.1) }, { kind: 'pr',"
        " url: 'https://github.com/o/app/pull/4', title: 'app#4', role: 'created',"
        " status: 'merged', changed_at: at(0.2) }] };\n"
        "return a.deliverablesOf(task).map((item) =>"
        " Math.round((Date.now() - Date.parse(item.at)) / 60000));"
    )
    # A PR the server gives with its status, its receipt not here: so too, not its task's time.
    assert found == [6, 12]


def test_what_needs_a_look_of_any_task_is_above_another_tasks_merged_prs():
    """E58: one task's six merged PRs sat above another task's PR with failing CI (a list 300px
    high showed only the merged ones)."""
    found = run_js(
        DOM
        + ARTIFACTS
        + NINE
        + "const merged = [1, 2, 3, 4, 5, 6].map((n) => pr(n, 'merged', 0.1 * n));\n"
        "const failing = { ...runs[3], url: 'https://flyte.x/a/failed', title: 'Backfill' };\n"
        "const shipping = task('a', 'Ship the export', failing, ...merged);\n"
        "const cache = { ...task('b', 'Tune the cache', pr(8, 'CI failing', 5)),"
        " updated_at: at(5) };\n"
        "const onlyMerged = task('c', 'Tidy the docs',"
        " ...[11, 12].map((n) => pr(n, 'merged', 1)));\n"
        "const { rows } = a.artifactItems([shipping, cache, onlyMerged], () => {});\n"
        "return rows.map((row) => { const node = row.build();"
        " return walk(node).find((p) => String(p.className).split(' ').includes('a-name'))"
        "?.textContent; });"
    )
    assert found[:2] == ["Backfill", "app#8 · Change 8"], found
    # The merged ones after both, a task's together.
    assert found[2:8] == [f"app#{n} · Change {n}" for n in (1, 2, 3, 4, 5, 6)], found
    assert found[8:] == ["app#11 · Change 11", "app#12 · Change 12"], found


def test_documents_runs_and_ci_builds_are_known_wherever_they_are_served():
    """E71: a run on this computer or its network was a plain link on its card. E73: a CI
    build was not known for a run."""
    urls = {
        "http://localhost:8080/dags/local/grid": ["run", "Airflow · local"],
        "http://10.1.2.9:8080/dags/nightly/grid": ["run", "Airflow · nightly"],
        "https://flyte.example.local/console/projects/a/domains/b/executions/c1": [
            "run",
            "Flyte · c1",
        ],
        "https://ci.acme.dev/job/deploy/123/": ["run", "Jenkins · deploy #123"],
        "https://gitlab.acme.dev/acme/app/-/pipelines/901": ["run", "GitLab · app pipeline 901"],
        "https://gitlab.acme.dev/acme/app/-/jobs/902": ["run", "GitLab · app job 902"],
        "https://buildkite.com/acme/app/builds/678": ["run", "buildkite · build 678"],
        "https://dev.azure.com/acme/app/_build/results?buildId=555": [
            "run",
            "Azure DevOps · build 555",
        ],
        "http://localhost:3000/app": ["link", "localhost · app"],
    }
    found = run_js(
        f"return {json.dumps(list(urls))}.map((u) => [t.linkKind(u).kind, t.linkKind(u).label]);"
    )
    assert dict(zip(urls, found, strict=True)) == urls
