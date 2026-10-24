"""从 GitHub 安装 mattpocock/skills 到 DSH 技能目录。

    python tools/install_skills.py                 # 装到 ~/.dsh/skills（默认剥掉 Claude 专用标记）
    python tools/install_skills.py --dest DIR
    python tools/install_skills.py --keep-disable-flag   # 保留 disable-model-invocation
    python tools/install_skills.py --dry-run

为什么要剥 `disable-model-invocation`：Claude Code 用这个标记表示"只允许用户手动 /调用"，
DSH 的技能目录只会把未标记的技能暴露给模型，保留它这 23 个技能在 DSH 里就等于装了看不见。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = "mattpocock/skills"
BRANCH = "main"
FLAG_RE = re.compile(r"^disable-model-invocation:\s*true\s*$", re.M)


def fetch_tarball(repo: str, branch: str) -> bytes:
    url = f"https://api.github.com/repos/{repo}/tarball/{branch}"
    req = urllib.request.Request(url, headers={"User-Agent": "dsh-skill-installer",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def default_dest() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh" / "skills"


def install(data: bytes, dest: Path, keep_flag: bool = False, dry_run: bool = False) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skills: set[str] = set()
    stripped = 0

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = tf.getnames()
        root = names[0].split("/")[0] if names else ""
        for member in tf.getmembers():
            if not member.isfile() or not member.name.startswith(f"{root}/skills/"):
                continue
            parts = member.name[len(f"{root}/skills/"):].split("/")
            if len(parts) < 3:          # 分类目录的 README，跳过
                continue
            category, name = parts[0], parts[1]
            if category == "deprecated":
                continue
            payload = tf.extractfile(member).read()
            if not keep_flag and member.name.endswith("SKILL.md"):
                text = payload.decode("utf-8", "replace")
                new_text = FLAG_RE.sub("", text)
                if new_text != text:
                    stripped += 1
                    payload = new_text.encode("utf-8")
            skills.add(name)
            target = dest / name / "/".join(parts[2:])
            written.append(str(target))
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    return {"skills": sorted(skills), "files": len(written), "stripped": stripped}


def main() -> int:
    parser = argparse.ArgumentParser(description="把 mattpocock/skills 安装到 DSH")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument("--dest", default=None)
    parser.add_argument("--keep-disable-flag", action="store_true",
                        help="保留 disable-model-invocation（DSH 里这些技能将不出现在模型可见目录）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest()
    print(f"→ {args.repo}@{args.branch} → {dest}")
    data = fetch_tarball(args.repo, args.branch)
    print(f"  下载完成：{len(data) / 1024:.0f} KB")
    result = install(data, dest, keep_flag=args.keep_disable_flag, dry_run=args.dry_run)

    print(f"  技能 {len(result['skills'])} 个 / 文件 {result['files']} 个"
          + ("（dry-run，未写盘）" if args.dry_run else ""))
    if result["stripped"] and not args.keep_disable_flag:
        print(f"  已为 {result['stripped']} 个技能移除 disable-model-invocation（DSH 才能看见）")
    for name in result["skills"]:
        print(f"    - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
