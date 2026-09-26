#!/usr/bin/env python3
"""
pack_for_nas.py — 打包 workbuddy2api 供 NAS 部署（含 tar.gz 直接构建约定）。

为什么需要这个脚本（而不是手敲 tar 命令）：
  1. **中文 `--exclude` 在 PowerShell 下会被编码搞坏** —— 实测 `--exclude=参考`
     被解析成 `a\\Local`，tar 直接报 `Couldn't visit directory`。
     用 Python 写排除规则，不经过 shell，彻底绕开这个问题。
  2. **Windows 的路径分隔符** 会让包内出现 `workbuddy2api\\converter.py` 这种
     反斜杠路径，Linux/Docker 解压后是一个**带反斜杠的文件名**，不是目录 → 构建必失败。
     这里统一转成 `/`。
  3. **`context: ./wb2api-<ver>.tar.gz` 要求包根 = 项目根**。
     若保留 `workbuddy2api/` 前缀，Dockerfile 里的 `COPY requirements.txt .`
     会找不到文件。所以这里**不写前缀**，直接扁平打包。
  4. 顺带**兜住凭据**：`.info` / `*.token` / `.key` 一律不进包。
  5. **包名带版本**（`wb2api-<ver>.tar.gz`，ver 与 CodeBuddy CLI 对齐），
     并在包内附一份 `VERSION`。

版本号从哪来（**单一真源**）：
    `model_registry.CLI_UA_VERSION` + `model_registry.SERVICE_BUILD`。
    - CLI_UA_VERSION：服务对上游声明 `User-Agent: CLI/<ver> CodeBuddy/<ver>` 的版本，
      只在「对齐上游 CLI」的契约变化时才动；
    - SERVICE_BUILD：**每次服务代码实质变更 +1** —— 镜像 tag / 包名必须跟着变。
      tag 固定会让 `docker compose up -d` 复用旧镜像，改了代码不生效（踩过两次）；
      tag 变了 Docker 自然新建镜像，不再依赖 --no-cache 靠人记。
    - 对外版本串 = `f"{CLI_UA_VERSION}-b{SERVICE_BUILD}"`（如 `2.155.0-b2`），
      包名、包内 VERSION、镜像 tag 三者由同一常量驱动，不可能漂移。

产物默认落在仓库**上一级**（避免被下一次打包 walk 到自己，形成自我嵌套）。

⚠️ 重要：**群晖 Container Manager 不支持 .tar.gz 作为 build context**
（报 `unable to prepare context: context must be a directory`）。
它的构建器不认 tar context，只认目录。所以往群晖部署有两条路：

  路线 1（推荐，一步到位）：**在本地打包的同时直接解压出源码目录**，
      然后把整个目录拷到群晖，compose 里用 `context: .`。
          python3 pack_for_nas.py --extract ../wb2api-src
      → 得到 ../wb2api-src/（扁平源码树，含 VERSION）+ ../wb2api-<ver>.tar.gz（备份/传输用）

  路线 2（只传一个文件）：把 tar.gz 拷到群晖，**在群晖上手工解压**再构建：
          tar -xzf wb2api-2.155.0.tar.gz -C .
      然后用 `context: .`。

  群晖上**不要**写 `context: ./wb2api-<ver>.tar.gz` —— 必失败。

用法：
    python3 pack_for_nas.py                        # 生成 ../wb2api-<ver>.tar.gz
    python3 pack_for_nas.py --extract ../wb2api-src  # 同时解压出源码目录（群晖用这个）
    python3 pack_for_nas.py -o D:\\x.tar.gz          # 指定输出
    python3 pack_for_nas.py --list                 # 只列清单，不打包

运行：python3 pack_for_nas.py --extract ../wb2api-src
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import pathlib
import sys
import tarfile
import os

HERE = pathlib.Path(__file__).resolve().parent

#: 不打包的目录名（构建/缓存/凭据/data 都不需要进包）
EXCLUDE_DIRS = {".git", "__pycache__", ".idea", ".workbuddy", "converter-logs", "data", "logs", "auth", "auths"}

#: 不打包的扩展名 —— **凭据类必须挡住**，另外把包自身也排除（防自我嵌套）。
#: 注意 `.gz` 一并挡住：带版本号的包名是 `wb2api-2.155.0.tar.gz`，
#: 若只挡 `.tar.gz` 就会被下次打包 walk 进自己的**上一版**，越滚越大。
EXCLUDE_SUFFIX = (".info", ".log", ".key", ".token", ".tar.gz", ".tgz", ".gz", ".zip", ".pyc")

#: 必须存在于包里的文件，缺任何一个都说明打错了
REQUIRED = ("Dockerfile", "requirements.txt", "converter.py", "environments.py", "compose.yaml")


def upstream_version() -> str:
    """对外版本串（`2.155.0-b2` 形态）。详见 `_load_versions`。"""
    return _load_versions()[0]


def _load_versions() -> tuple[str, str]:
    """加载版本号，返回 (对外版本串, CLI 版本)。

    **单一真源**：直接读 `model_registry.CLI_UA_VERSION` 与 `model_registry.SERVICE_BUILD`
    （及拼好的 `SERVICE_VERSION`）。前者是本服务对上游声明
    `User-Agent: CLI/<ver> CodeBuddy/<ver>` 的版本；后者每次服务代码变更 +1。
    包名与镜像 tag 用对外版本串，想换版本只改 model_registry 一处。

    实现上按**文件路径**加载，绕开 `sys.path` 依赖（本脚本可能从任意 cwd 调用）。
    两个必须做对的地方：
      - `sys.path` 要临时加上模块所在目录，否则它 import 的 `environments` 找不到；
      - 必须**注册进 `sys.modules`**，否则 `@dataclass` 解析注解时
        用 `sys.modules[cls.__module__]` 会拿到 None，报
        `'NoneType' object has no attribute '__dict__'`（实测踩过）。
    """
    src = HERE / "model_registry.py"
    name = "_wb2api_version_src"
    spec = importlib.util.spec_from_file_location(name, src)
    if spec is None or spec.loader is None:
        raise SystemExit(f"❌ 读不到版本号：{src}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # ← dataclass 需要它
    added = str(HERE) not in sys.path
    if added:
        sys.path.insert(0, str(HERE))
    try:
        spec.loader.exec_module(mod)
        ver = getattr(mod, "CLI_UA_VERSION", None)
        build = getattr(mod, "SERVICE_BUILD", None)
        svc = getattr(mod, "SERVICE_VERSION", None)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"❌ 解析版本号失败（{src}）：{e}") from e
    finally:
        sys.modules.pop(name, None)
        if added:
            try:
                sys.path.remove(str(HERE))
            except ValueError:
                pass

    if not isinstance(ver, str) or not ver.strip():
        raise SystemExit("❌ model_registry.CLI_UA_VERSION 缺失或为空")
    # SERVICE_VERSION 在 model_registry 里已拼好；这里再验一次存在性，
    # 防止老版本 model_registry 没有 build 号时静默退回裸 CLI 版本（tag 又不变了）。
    if not isinstance(svc, str) or not svc.strip() or not isinstance(build, int):
        raise SystemExit("❌ model_registry.SERVICE_BUILD / SERVICE_VERSION 缺失 —— 请同步升级 model_registry.py")
    return svc, ver


def archive_name(version: str | None = None) -> str:
    """包文件名：`wb2api-<version>.tar.gz`，默认版本与 CLI UA 对齐。"""
    return f"wb2api-{version or upstream_version()}.tar.gz"


def _skip(fp: pathlib.Path) -> bool:
    """该路径是否应排除。"""
    if any(part in EXCLUDE_DIRS for part in fp.parts):
        return True
    return str(fp).lower().endswith(EXCLUDE_SUFFIX)


def build_archive(out: pathlib.Path, *, only_list: bool = False) -> tuple[int, int, str]:
    """打包并返回 (文件数, 字节数, sha256)。only_list=True 时只统计不落盘。"""
    src = HERE
    entries: list[tuple[pathlib.Path, str]] = []

    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in sorted(files):
            fp = pathlib.Path(root) / f
            if _skip(fp):
                continue
            # ★ 扁平化：包根 = 项目根；且统一用 `/`（Windows 的反斜杠会让 Linux 解出怪文件名）
            arc = str(fp.relative_to(src)).replace(os.sep, "/")
            entries.append((fp, arc))

    names = {arc for _, arc in entries}
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        raise SystemExit(f"❌ 打包内容缺少必需文件：{missing}")

    if only_list:
        return len(entries), 0, ""

    # 先清空旧包，避免 walk 到半成品
    if out.exists():
        out.write_bytes(b"")

    # 包内附一份 VERSION：在 NAS 上 `cat VERSION` 就能确认跑的是哪一版，
    # 不必回头对 SHA256（固定 image tag 会掩盖重建，靠文件名+这个文件最省事）。
    svc, ver = _load_versions()
    # 文件数要算上 VERSION 自己，否则包内记的数字与实际差 1（踩过）。
    total_files = len(entries) + 1
    version_blob = (
        f"{svc}\n"
        f"# workbuddy2api 部署包版本\n"
        f"# 服务版本 = CLI 版本 + 构建号（= model_registry.SERVICE_VERSION）\n"
        f"# CLI 版本：{ver}（与 CodeBuddy CLI 对齐，驱动上游 UA）\n"
        f"# 归档：{out.name}\n"
        f"# 文件数：{total_files}\n"
    ).encode("utf-8")

    total = 0
    with tarfile.open(out, "w:gz") as tf:
        for fp, arc in entries:
            ti = tf.gettarinfo(str(fp), arcname=arc)
            # ★ 归一化元信息，让打包**可复现**。
            # 不清掉 mtime/uid/gid 的话，同样内容每打一次 SHA256 都不同
            # （实测：连打两次内容 diff -r 完全一致，但 SHA 不同）——
            # 这样 SHA256 就丧失了「对照校验」的意义，只剩自欺。
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mode = 0o644
            with open(fp, "rb") as fh:
                tf.addfile(ti, fh)
            total += fp.stat().st_size

        tv = tarfile.TarInfo("VERSION")
        tv.size = len(version_blob)
        tv.mtime = 0
        tv.uid = tv.gid = 0
        tv.uname = tv.gname = "root"
        tv.mode = 0o644
        tf.addfile(tv, io.BytesIO(version_blob))
        total += len(version_blob)
        n_entries = total_files

    data = out.read_bytes()
    return n_entries, len(data), hashlib.sha256(data).hexdigest()


def extract_archive(archive: pathlib.Path, dest: pathlib.Path) -> tuple[int, int]:
    """把包解到 dest（扁平源码树），返回 (文件数, 字节数)。

    存在的理由：**群晖 Container Manager 不接受 .tar.gz 作为 build context**
    （`unable to prepare context: context must be a directory`）。
    所以「传到 NAS」这一步之后，最终必须落成一个**目录**。
    与其让用户上 NAS 敲 tar，不如本地就解好，直接拷目录过去。

    安全：拒绝任何绝对路径或含 `..` 的成员（防目录穿越）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.replace("\\", "/").lstrip("/")
            if ".." in pathlib.PurePosixPath(name).parts:
                raise SystemExit(f"❌ 包内存在非法路径，拒绝解压：{m.name}")
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            target.write_bytes(src.read())
            n += 1
            total += m.size
    return n, total


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 workbuddy2api 供 NAS 部署")
    ap.add_argument(
        "-o", "--output",
        help="输出路径，默认 <仓库上一级>/wb2api-<CodeBuddy版本>.tar.gz",
    )
    ap.add_argument("--list", action="store_true", help="只列清单，不实际打包")
    ap.add_argument(
        "--extract",
        metavar="DIR",
        help="打包后同时解压到该目录（群晖不支持 tar context，部署得用目录）",
    )
    args = ap.parse_args()

    svc, cli_ver = _load_versions()
    # 默认包名带版本，且与 CodeBuddy CLI 版本一致 —— 版本号只从 CLI_UA_VERSION 来。
    out = pathlib.Path(args.output) if args.output else HERE.parent / archive_name(svc)
    n, size, digest = build_archive(out, only_list=args.list)

    if args.list:
        print(f"将打包 {n} 个文件（未落盘）")
        return 0

    print(f"✅ 打包完成：{out}")
    print(f"   服务版本：{svc}（= CLI {cli_ver} + 构建号；换代码必须 bump SERVICE_BUILD）")
    print(f"   文件数：{n}")
    print(f"   大小  ：{size:,} B ({size / 1024:.1f} KB)")
    print(f"   SHA256：{digest}")

    if args.extract:
        dest = pathlib.Path(args.extract)
        en, esize = extract_archive(out, dest)
        print()
        print(f"✅ 已解压到目录：{dest.resolve()}")
        print(f"   文件数：{en}")
        print(f"   大小  ：{esize:,} B")
        print()
        print("群晖上把**这个目录**拷过去，compose 里用：")
        print("   build:")
        print("     context: .")
        print("     dockerfile: Dockerfile")
        print(f"   image: workbuddy2api:{svc}   # 必须用 --extract 打印的这个 tag")
        print()
        print(f"⚠️ 不要写 context: ./{out.name} —— 群晖构建器不认 tar，会报")
        print("   unable to prepare context: context must be a directory")
        print()
        print(f"上机后确认版本：cat VERSION   # 应为 {svc}")
        return 0

    print()
    print("包内已扁平化（包根即项目根）。")
    print("⚠️ 群晖**不认** .tar.gz 作为 context，只认目录：")
    print(f"   推荐：python3 pack_for_nas.py --extract ../wb2api-src  然后拷目录过去")
    print(f"   或者：把 {out.name} 拷到群晖后先 `tar -xzf {out.name} -C .` 再构建")
    return 0


if __name__ == "__main__":
    sys.exit(main())
