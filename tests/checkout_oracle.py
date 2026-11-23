"""A soundness oracle for the check of what a command writes in the owner's checkout given to
read (policy.checkout_write, through policy.owners_checkout), against real bash.

Lines of a small, safe vocabulary (writes and reads by redirection, `tee`, `cp`, `mv`, `rm`,
`touch`, `mkdir`, `chmod`, `ln`, `sed -i`, `dd`, `sort -o`; git's add, commit, checkout, stash,
branch, config, status, diff, log; `cd`, subshells, `sh -c`, `eval`, loops, `find -delete`,
`xargs`, a script of the checkout's own) run in `bash --norc --noprofile -c`, each in a fresh
copy of a sandbox: a git repository (the checkout given to read, `kept`) and a folder of the
worker's (`own`) beside it, the line run in one or the other. What it changed in `kept` is
read after (its files, and as git says them: its index, HEAD, branches, stashes, config).
The property: a line the check lets through changed nothing there.

Every path a line names is in the sandbox (relative ones, `../kept/…`, or the sandbox's own
absolute ones), and its HOME too. `python tests/checkout_oracle.py --cases N --seed S` runs N
generated lines and prints the counts.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import random
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

READS = ["AGENTS.md", "src/app.py", "tool.py", "{K}/AGENTS.md", "{K}/src/app.py",
         "../kept/AGENTS.md", "{O}/mine.txt", "mine.txt"]  # fmt: skip
WRITES = ["AGENTS.md", "new.txt", "src/new.py", "{K}/AGENTS.md", "{K}/src/n.py",
          "../kept/n.txt", "../own/out.txt", "{O}/out.txt", "out.txt",
          "{K}/../own/o.txt"]  # fmt: skip
SIMPLE = [
    "echo y > {w}", "echo y >> {w}", "printf 'y\\n' > {w}", "cat {r} > {w}", ": > {w}",
    "cat {r}", "head -1 {r}", "grep -c x {r}", "wc -l {r}", "sed -n 1p {r}",
    "sed -i.bak s/x/y/ {w}", "echo y | tee {w}", "echo y | tee -a {w} > /dev/null",
    "cp {r} {w}", "mv {w} {w2}", "rm -f {w}", "touch {w}", "mkdir -p {w}.d", "chmod 600 {w}",
    "ln -sf {r} {w}", "dd if=/dev/zero of={w} count=1 2>/dev/null", "sort {r} -o {w}",
    "sort {r} > {w}", "ls {r} 2>/dev/null", "test -f {r} && echo yes",
    "git status --short", "git diff --stat", "git log --oneline -1", "git branch",
    "git add -A", "git commit -qm m --allow-empty", "git checkout -qb b{n}", "git stash -q",
    "git branch b{n}", "git config user.name q{n}", "git -C {K} add -A", "git -C {K} status",
    "git stash list", "git config user.name",
    "find {K} -name '*.bak' -delete", "find . -name n.txt | xargs rm -f",
    "ls {K} | xargs -I{{}} echo {{}}", "for f in {K}/*.md; do echo y >> $f; done",
    "for f in *.md; do cat $f; done", "python3 tool.py", "python3 {K}/tool.py",
]  # fmt: skip
TOOL = "from pathlib import Path\nPath('w.txt').write_text('w')\n"


def made(rng: random.Random, depth: int = 0) -> str:
    """A command of the vocabulary, its files chosen; at times in a shell of its own or an eval,
    or after a cd."""
    line = rng.choice(SIMPLE).format(
        r=rng.choice(READS),
        w=rng.choice(WRITES),
        w2=rng.choice(WRITES),
        n=rng.randrange(99),
        K="{K}",
        O="{O}",
    )
    roll = rng.random()
    if depth < 1 and roll < 0.1 and "'" not in line:
        line = f"sh -c '{line}'"
    elif depth < 1 and roll < 0.18 and "'" not in line:
        line = f"eval '{line}'"
    elif roll < 0.3:
        # (Into the sandbox's own folders only: a `cd ..` twice would leave it. `gone` is not
        # there: the cd fails, and what runs then runs where the line was.)
        to = rng.choice(["{K}", "{O}", "{K}/src", "{K}/src/..", "{O}/gone", "{K}/gone"])
        line = f"cd {to} {rng.choice(['&&', ';', '||'])} {line}"
    elif roll < 0.36:
        line = f"(cd {rng.choice(['{K}', '{O}', '{O}/gone'])} && {line})"
    elif roll < 0.4:
        line = (
            f"pushd {rng.choice(['{K}', '{O}', '{O}/gone'])} > /dev/null; {line}; popd > /dev/null"
        )
    elif depth < 1 and roll < 0.43 and "'" not in line:
        line = f"sh -c 'cd {rng.choice(['{O}', '{O}/gone'])} || {line}'"
    return line


@dataclass
class Generator:
    seed: int

    def __post_init__(self):
        self.rng = random.Random(self.seed)  # noqa: S311  (a fixed sample, not a secret)

    def line(self) -> tuple[str, str]:
        """A line of one to three commands, and where it runs ("kept" or "own")."""
        parts = [made(self.rng) for _ in range(self.rng.randint(1, 3))]
        joined = parts[0]
        for part in parts[1:]:
            joined += self.rng.choice(["; ", " && ", " || ", "\n"]) + part
        return joined, self.rng.choice(["kept", "own"])


def sandbox(root: Path) -> tuple[Path, Path]:
    """The template: `kept`, a repository with a commit (and a script of its own), `own` beside
    it."""
    kept, own = root / "kept", root / "own"
    (kept / "src").mkdir(parents=True)
    own.mkdir()
    (kept / "AGENTS.md").write_text("x\nhello\n")
    (kept / "src" / "app.py").write_text("x = 1\n")
    (kept / "tool.py").write_text(TOOL)
    (own / "mine.txt").write_text("x\n")
    git = ["git", "-C", str(kept), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(kept)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return kept, own


def state(kept: Path) -> str:
    """What `kept` holds: its files (not git's own), and as git says them its index, HEAD,
    branches, stashes and config."""
    found = hashlib.sha256()
    for path in sorted(kept.rglob("*")):
        relative = path.relative_to(kept)
        if relative.parts[0] == ".git":
            continue
        kind = "l" if path.is_symlink() else "d" if path.is_dir() else "f"
        body = str(path.readlink()) if kind == "l" else path.read_bytes() if kind == "f" else ""
        mode = path.lstat().st_mode & 0o777
        found.update(f"{relative}\0{kind}\0{mode}\0".encode())
        found.update(body if isinstance(body, bytes) else str(body).encode())
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    for asked in (["ls-files", "-s"], ["symbolic-ref", "-q", "HEAD"], ["for-each-ref"],
                  ["stash", "list"], ["config", "--local", "--list"]):  # fmt: skip
        done = subprocess.run(["git", "-C", str(kept), *asked], capture_output=True, env=env)
        found.update(done.stdout)
    return found.hexdigest()


@dataclass
class Outcome:
    line: str
    where: str
    verdict: str  # "writes", "asks" or "".
    changed: bool


def run(policy, cases: list[tuple[str, str]], tmp: Path, workers: int = 4) -> list[Outcome]:
    """Each line, in a fresh copy of the sandbox, in real bash; and what the check says of it."""
    template = tmp / "template"
    sandbox(template)
    before = state(template / "kept")

    def one(item: tuple[int, tuple[str, str]]) -> Outcome:
        number, (line, where) = item
        root = tmp / f"run{number}"
        shutil.copytree(template, root, symlinks=True)
        kept, own = root / "kept", root / "own"
        # Quoted, as a worker writes a path with a space in it (the tests' folders have one).
        text = line.replace("{K}", f'"{kept}"').replace("{O}", f'"{own}"')
        cwd = kept if where == "kept" else own
        verdict = policy.checkout_write(text, str(cwd), (str(kept),))
        home = root / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "LC_ALL": "C",
               "GIT_CEILING_DIRECTORIES": str(tmp)}  # fmt: skip
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(["bash", "--norc", "--noprofile", "-c", text], cwd=cwd, env=env,
                           capture_output=True, timeout=20, stdin=subprocess.DEVNULL)  # fmt: skip
        changed = state(kept) != before
        shutil.rmtree(root, ignore_errors=True)
        return Outcome(line, where, verdict, changed)

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, enumerate(cases)))


def seeds() -> list[tuple[str, str]]:
    """Each command of the vocabulary once, as it is, in each place."""
    rng = random.Random(1)  # noqa: S311  (a fixed sample)
    lines = []
    for command in SIMPLE:
        line = command.format(r=rng.choice(READS), w=rng.choice(WRITES), w2=rng.choice(WRITES),
                              n=1, K="{K}", O="{O}")  # fmt: skip
        lines += [(line, "kept"), (line, "own")]
    # A cd that fails: what runs then runs where the line was (`../kept` from the worker's own).
    for line in ("cd {O}/gone && cat x || echo y > ../kept/n.txt", "cd {O}/gone; touch ../kept/n",
                 "cd {O}/gone || touch ../kept/n", "(cd {O}/gone; touch ../kept/n)",
                 "pushd {O}/gone; touch ../kept/n; popd", "sh -c 'cd {O}/gone || touch ../kept/n'",
                 "cd {O}/gone || exit; touch ../kept/n", "cd {O}/gone; touch n"):  # fmt: skip
        lines += [(line, "kept"), (line, "own")]
    return lines


def rates(outcomes: list[Outcome]) -> str:
    changed = [o for o in outcomes if o.changed]
    unsound = [o for o in changed if not o.verdict]
    refused = [o for o in outcomes if o.verdict]
    needless = [o for o in refused if not o.changed]
    return (
        f"{len(outcomes)} lines, {len(changed)} changed the checkout, {len(refused)} refused or "
        f"asked ({len(needless)} of them changed nothing), {len(unsound)} unsound"
    )


if __name__ == "__main__":
    import argparse
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from chiefofstaff import policy as checked

    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument("--seed", type=int, default=0)
    arguments = parser.parse_args()
    generator = Generator(arguments.seed)
    cases = [generator.line() for _ in range(arguments.cases)] + seeds()
    with tempfile.TemporaryDirectory() as folder:
        outcomes = run(checked, cases, Path(folder))
    for outcome in outcomes:
        if outcome.changed and not outcome.verdict:
            print("UNSOUND", outcome.where, repr(outcome.line))  # noqa: T201
    print(rates(outcomes))  # noqa: T201
