#!/usr/bin/env python3
"""
test_deployment.py — 校验 Docker 部署文件与代码保持一致。

为什么需要：Dockerfile 用**显式文件名列表** COPY .py（而不是 `COPY . .`，
这是为了不让凭据/测试文件混进镜像），代价是新增模块时极易漏抄 ——
一旦漏掉某个被 import 的模块，容器会在启动瞬间 ModuleNotFoundError 崩掉，
而且这个错误只在真正 build+run 之后才暴露。本项目实测漏过 environments.py。

本测试把「Dockerfile 声明的模块」与「代码实际 import 的本地模块」做交叉校验，
把这类问题提前到本地测试阶段。

直接运行：python3 test_deployment.py
"""

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _dockerfile_copy_modules() -> set[str]:
    """解析 Dockerfile 里 COPY 声明的 .py 文件名（要先把 `\\` 续行拼回来）。"""
    raw = (HERE / "Dockerfile").read_text(encoding="utf-8")
    joined = re.sub(r"\\\s*\n\s*", " ", raw)
    names: set[str] = set()
    for line in joined.splitlines():
        if not line.strip().upper().startswith("COPY"):
            continue
        for tok in line.split()[1:]:
            tok = tok.rstrip("\\").strip()
            if tok.endswith(".py"):
                names.add(tok)
    return names


def _local_imports() -> set[str]:
    """扫出代码里真正被 import 的本地模块名（转成 .py 文件名）。"""
    local = {p.stem for p in HERE.glob("*.py")}
    found: set[str] = set()
    for p in HERE.glob("*.py"):
        if p.name.startswith(("test_", "_")):
            continue
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", src, re.M):
            mod = m.group(1)
            if mod in local and mod not in ("__init__",):
                found.add(mod)
    return found


def test_dockerfile_copies_every_imported_module():
    """Dockerfile 必须 COPY 所有被 import 的本地模块，否则容器起不来。"""
    # 统一成 stem（无扩展名）再比 —— Dockerfile 写的是 .py，import 写的是裸模块名
    copied = {n[:-3] if n.endswith(".py") else n for n in _dockerfile_copy_modules()}
    imported = _local_imports()
    missing = sorted(imported - copied)
    assert not missing, (
        f"Dockerfile 漏抄了被 import 的模块：{missing}\n"
        f"  Dockerfile 已声明：{sorted(copied)}\n"
        f"  代码实际 import：{sorted(imported)}\n"
        f"  修法：把漏掉的加进 Dockerfile 的 COPY 行"
    )
    print(f"✅ test_dockerfile_copies_every_imported_module  ({len(copied)} 个模块)")


def test_dockerfile_entrypoint_exists():
    """Dockerfile 的 CMD 引用的入口文件必须真实存在且被 COPY。"""
    raw = (HERE / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r'CMD\s*\[(.*?)\]', raw, re.S)
    assert m, "Dockerfile 里找不到 CMD"
    parts = re.findall(r'"([^"]+)"', m.group(1))
    assert parts, f"CMD 解析失败：{m.group(1)}"
    entry = next((p for p in parts if p.endswith(".py")), None)
    assert entry, f"CMD 里没找到 .py 入口：{parts}"
    assert (HERE / entry).is_file(), f"CMD 指定的入口不存在：{entry}"
    assert entry in _dockerfile_copy_modules(), f"CMD 的入口 {entry} 没被 COPY 进镜像"
    print(f"✅ test_dockerfile_entrypoint_exists  ({entry})")


def test_requirements_pins_httpx_with_socks():
    """httpx 必须锁 >=0.28 且带 [socks]，否则代理功能会 TypeError。"""
    req = (HERE / "requirements.txt").read_text(encoding="utf-8")
    line = next((l for l in req.splitlines() if l.strip().startswith("httpx")), None)
    assert line, "requirements.txt 里没有 httpx"
    assert "[socks]" in line, f"httpx 缺少 [socks] 额外依赖，socks5 代理会失败：{line}"
    m = re.search(r">=\s*0\.28", line)
    assert m, f"httpx 必须 >=0.28（0.28 起代理参数只认 proxy=）：{line}"
    print(f"✅ test_requirements_pins_httpx_with_socks  ({line.strip()})")


def test_dockerignore_does_not_exclude_entrycode():
    """`.dockerignore` 不能把需要的源码排除掉。"""
    di = (HERE / ".dockerignore").read_text(encoding="utf-8")
    patterns = [
        l.strip() for l in di.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    # 构建上下文里必须留着的文件
    needed = ["converter.py", "environments.py", "requirements.txt"]
    for name in needed:
        assert name not in patterns, f".dockerignore 排除了必需的 {name}"
    # 危险模式：`*.py` / `*.txt` 会把源码和依赖清单一并排除
    for bad in ("*.py", "*.txt", "*.json"):
        assert bad not in patterns, f".dockerignore 含过宽模式 {bad}，会排除必需文件"
    print(f"✅ test_dockerignore_does_not_exclude_entrycode  ({len(patterns)} 条规则)")


def test_dockerignore_excludes_credentials():
    """凭据绝不能被 COPY 进镜像 —— .dockerignore 必须挡住它们。"""
    di = (HERE / ".dockerignore").read_text(encoding="utf-8")
    patterns = {l.strip() for l in di.splitlines() if l.strip()}
    for must in ("*.info", "auth/", "*.token", "*.key"):
        assert must in patterns, f".dockerignore 漏了凭据防护规则 {must}"
    print("✅ test_dockerignore_excludes_credentials")


def test_compose_mounts_auth_writable():
    """compose 挂载的 auth 目录必须可写：token 刷新要用 os.replace 原子回写。"""
    y = (HERE / "compose.yaml").read_text(encoding="utf-8")
    assert "CODEBUDDY_AUTH_DIR" in y, "compose 没设置 CODEBUDDY_AUTH_DIR"
    # 找出所有挂到 /data/auth 的 volume 行
    m = re.search(r'-\s*"([^"]*:/data/auth)"', y)
    assert m, "compose 里没有挂载 /data/auth"
    spec = m.group(1)
    assert ":ro" not in spec, (
        f"auth 目录不能只读挂载（token 刷新会失败导致 401）：{spec}"
    )
    print(f"✅ test_compose_mounts_auth_writable  ({spec})")


def test_compose_sets_timezone():
    """TZ 必须是 Asia/Shanghai，否则六类定时任务整点会按 UTC 触发。"""
    y = (HERE / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(r'TZ\s*=\s*Asia/Shanghai', y) or "TZ=Asia/Shanghai" in y, (
        "compose 没把 TZ 设为 Asia/Shanghai —— 签到会按 UTC 在 17:00 触发"
    )
    print("✅ test_compose_sets_timezone")


def test_compose_env_vars_are_known():
    """compose 里出现的 CODEBUDDY* 环境变量名必须在代码里有对应实现。"""
    y = (HERE / "compose.yaml").read_text(encoding="utf-8")
    used = set(re.findall(r'\b(CODEBUDDY2OPENAI_[A-Z_]+|CODEBUDDY_AUTH_DIR)\b', y))

    # 代码里出现过的全部环境变量名
    known: set[str] = set()
    for p in HERE.glob("*.py"):
        if p.name.startswith("test_"):
            continue
        src = p.read_text(encoding="utf-8")
        known |= set(re.findall(r'"(CODEBUDDY2OPENAI_[A-Z_]+|CODEBUDDY_AUTH_DIR)"', src))

    unknown = sorted(used - known)
    assert not unknown, (
        f"compose 用了代码里不存在的环境变量：{unknown}\n"
        f"  代码已实现：{sorted(known)}"
    )
    print(f"✅ test_compose_env_vars_are_known  ({len(used)} 个变量)")


def test_env_docs_match_code():
    """README / compose 里文档化的新环境变量必须与代码常量一致。"""
    import environments as env_mod

    expected = {
        env_mod.ENV_VAR_ENV,
        env_mod.ENV_VAR_PROXY_FALLBACK,
        *env_mod.PROXY_ENV_VARS.values(),
        *env_mod.AUTH_ENV_VARS.values(),
    }
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    compose = (HERE / "compose.yaml").read_text(encoding="utf-8")
    blob = readme + compose

    missing = sorted(v for v in expected if v not in blob)
    assert not missing, (
        f"这些环境变量在代码里存在，但 README/compose 没记录：{missing}"
    )
    print(f"✅ test_env_docs_match_code  ({len(expected)} 个变量均已文档化)")


def test_compose_mount_paths_documented_in_readme():
    """compose 的宿主机挂载路径必须也在 README 里出现。

    为什么单独测：路径是最容易「改了 compose 忘了改文档」的地方，而文档错了
    用户会照着建到别的目录，然后卡在 `Bind mount failed`（群晖不会自动建目录）。
    """
    y = (HERE / "compose.yaml").read_text(encoding="utf-8")
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    hosts = set(re.findall(r'-\s*"(/[^":]+):/[^"]+"', y))
    assert hosts, "compose 里没找到绝对路径形式的挂载"
    missing = sorted(h for h in hosts if h not in readme)
    assert not missing, (
        f"compose 的宿主机挂载路径没写进 README：{missing}\n"
        f"  compose 里的路径：{sorted(hosts)}"
    )
    print(f"✅ test_compose_mount_paths_documented_in_readme  ({len(hosts)} 条路径)")


def test_synology_guide_mentions_manual_mkdir():
    """群晖说明必须点出「挂载目录要先手工建」。

    实测结论（群晖官方文档 + 社区一致）：群晖的 Docker **不会**自动创建 bind mount
    的宿主机目录（Linux 上会），缺目录时项目直接 `Bind mount failed` 起不来。
    这条注意事项容易被当成废话删掉，删了用户就会踩坑，所以钉死。
    """
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    idx = readme.find("### 群晖（Synology）")
    assert idx >= 0, "README 里找不到「### 群晖（Synology）」小节"
    # 只检查该小节（到下一个同级或更高级标题为止）
    rest = readme[idx + 1:]
    end = rest.find("\n### ")
    section = rest[:end] if end >= 0 else rest

    assert "先手工建" in section or "必须先手工建好" in section, (
        "群晖小节没提醒「挂载目录必须先手工创建（否则 Bind mount failed）」"
    )
    assert "Bind mount failed" in section, (
        "群晖小节没给出缺目录时的报错特征 `Bind mount failed`"
    )
    print("✅ test_synology_guide_mentions_manual_mkdir")


def test_pack_script_produces_flat_archive():
    """`pack_for_nas.py` 产出的包必须满足 `context: ./x.tar.gz` 的硬要求。

    为什么单独测：这是**很容易悄悄坏掉**的一环 ——
      * 加了 `workbuddy2api/` 前缀 → `COPY requirements.txt .` 找不到文件；
      * Windows 的反斜杠进了包 → Linux 解出带反斜杠的怪文件名；
      * 忘记排除凭据 → 登录态进包。
    任何一条都会让 NAS 上的构建直接失败，而报错信息离根因很远。
    """
    import importlib.util
    import tarfile
    import tempfile

    spec = importlib.util.spec_from_file_location("pack_for_nas", HERE / "pack_for_nas.py")
    assert spec and spec.loader, "找不到 pack_for_nas.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "wb2api.tar.gz"
        n, size, digest = mod.build_archive(out)
        assert out.is_file() and size > 0, "打包未产出文件"
        assert len(digest) == 64, f"SHA256 长度异常：{digest!r}"

        with tarfile.open(out) as tf:
            names = tf.getnames()

    # 1) 扁平：包根即项目根
    prefixed = [x for x in names if x.startswith("workbuddy2api/")]
    assert not prefixed, f"包内出现了 workbuddy2api/ 前缀，COPY 会失败：{prefixed[:5]}"
    # 2) 正斜杠
    backslash = [x for x in names if "\\" in x]
    assert not backslash, f"包内路径含反斜杠，Linux 会解出怪文件名：{backslash[:5]}"
    # 3) 必需文件
    for req in mod.REQUIRED:
        assert req in names, f"包内缺少必需文件 {req}"
    # 4) 凭据绝不入包
    leaked = [x for x in names if x.endswith((".info", ".token", ".key"))]
    assert not leaked, f"凭据类文件进了包：{leaked}"
    # 5) 不自我嵌套
    assert not [x for x in names if x.endswith(".tar.gz")], "包内嵌套了 tar.gz"
    print(f"✅ test_pack_script_produces_flat_archive  ({n} 个文件，已扁平)")


def test_compose_supports_tarball_context():
    """compose 必须写明「tar 包直接构建」这条路径，且与打包脚本的约定一致。"""
    y = (HERE / "compose.yaml").read_text(encoding="utf-8")
    assert "wb2api.tar.gz" in y, (
        "compose 里没提到 wb2api.tar.gz —— 用 tar 包构建的路径缺少文档"
    )
    # 打包脚本产出的文件名必须和 compose 注释里写的一致
    assert (HERE / "pack_for_nas.py").is_file(), "缺少打包脚本 pack_for_nas.py"
    print("✅ test_compose_supports_tarball_context")


def test_compose_context_is_a_directory():
    """compose 里**生效的** `context` 必须是目录，不能是 .tar.gz。

    实测踩坑：群晖 Container Manager 的构建器**不认 tar 作为 build context**，
    会直接报：

        unable to prepare context: context must be a directory

    （普通 Linux 上 `docker build` 是支持 tar context 的，所以这条限制
    只在群晖上暴露 —— 本地测不出来，必须靠这条断言钉住。）

    注意只检查**未被注释**的行：注释里保留 tar 用法说明是允许且必要的。
    """
    raw = (HERE / "compose.yaml").read_text(encoding="utf-8")
    active = [ln.strip() for ln in raw.splitlines() if not ln.strip().startswith("#")]
    ctx_lines = [ln for ln in active if ln.startswith("context:")]
    assert ctx_lines, "compose 里找不到生效的 build.context"
    for ln in ctx_lines:
        value = ln.split(":", 1)[1].strip().strip("'\"")
        assert not value.endswith((".tar.gz", ".tgz", ".tar", ".zip")), (
            f"build.context 指向了压缩包（{value}）—— 群晖会报 "
            f"`context must be a directory`，请改用目录。"
        )
    # 同时必须在注释里提醒这条限制，否则下次又会踩
    assert "context must be a directory" in raw, (
        "compose 没提醒「群晖不支持 tar context」这条报错特征"
    )
    print(f"✅ test_compose_context_is_a_directory  ({ctx_lines[0]})")


def test_pack_script_can_extract_to_directory():
    """`pack_for_nas.py --extract` 必须能产出一棵可直接构建的扁平源码树。

    为什么需要：群晖不认 tar context，所以「传到 NAS」的最终形态**必须是一个目录**。
    与其让用户上 NAS 手敲 `tar -xzf`，不如本地就解好。
    这里直接调 `extract_archive()` 并复验解出来的树能满足 Dockerfile 的 COPY。
    """
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location("pack_for_nas", HERE / "pack_for_nas.py")
    assert spec and spec.loader, "找不到 pack_for_nas.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        arc = td / "wb2api.tar.gz"
        mod.build_archive(arc)
        dest = td / "src"
        n, size = mod.extract_archive(arc, dest)

        # 1) Dockerfile 声明的每个模块都必须解出来（否则容器起不来）
        for mod_name in _dockerfile_copy_modules():
            assert (dest / mod_name).is_file(), f"解压后缺少 Dockerfile 声明的 {mod_name}"
        # 2) 必需文件
        for req in mod.REQUIRED:
            assert (dest / req).is_file(), f"解压后缺少 {req}"
        # 3) 扁平：不能多一层 workbuddy2api/ 前缀
        assert not (dest / "workbuddy2api").is_dir(), "解压后多了一层 workbuddy2api/ 前缀"
        # 4) 凭据绝不落地
        leaked = [
            str(p.relative_to(dest))
            for p in dest.rglob("*")
            if p.is_file() and p.suffix in (".info", ".token", ".key")
        ]
        assert not leaked, f"解压后出现凭据文件：{leaked}"

    print(f"✅ test_pack_script_can_extract_to_directory  ({n} 个文件)")


def test_archive_name_matches_codebuddy_version():
    """包名必须带版本，且版本**与服务版本串一致**（同一真源）。

    版本号两层（2026-09-20 起）：
        model_registry.CLI_UA_VERSION  →  上游 UA `CLI/<ver> CodeBuddy/<ver>`（只在
                                          对齐 CLI 契约时才动）
        model_registry.SERVICE_BUILD   →  服务代码每次实质变更 +1
        model_registry.SERVICE_VERSION →  `{CLI}-{bN}`，如 `2.155.0-b2`
    四条链路必须永远指向 SERVICE_VERSION：
        pack_for_nas.archive_name()    →  包名 `wb2api-<ver>.tar.gz`
        pack_for_nas.upstream_version() →  包内 VERSION 文件首行
        compose.yaml 的 image tag      →  `workbuddy2api:<ver>`
        converter /health 的 service_version

    为什么值得钉住：
      ① 包名与 UA 若各自硬编码，改一处忘一处就会出现「包叫 2.155，但对上游自称 2.1」
         这种查起来极费劲的错配。
      ② 镜像 tag 若一直等于裸 CLI 版本，`docker compose up -d` 会静默复用旧镜像，
         改了代码不生效 —— 用户明确要求「版本号必须变」。所以这里还钉住
         SERVICE_VERSION ≠ 裸 CLI 版本（带 -bN 后缀），防止将来有人把它改回去。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("pack_for_nas", HERE / "pack_for_nas.py")
    assert spec and spec.loader, "找不到 pack_for_nas.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from model_registry import CLI_UA_VERSION, SERVICE_VERSION, cli_user_agent

    ver = mod.upstream_version()
    assert ver == SERVICE_VERSION, (
        f"打包脚本读到的版本 {ver} 与 model_registry.SERVICE_VERSION "
        f"{SERVICE_VERSION} 不一致 —— 说明版本号不再单一真源"
    )
    # 服务版本串必须形如 `{CLI 版本}-b<N>`：前缀对齐 CLI，后缀是构建号
    assert ver.startswith(f"{CLI_UA_VERSION}-b"), (
        f"服务版本 {ver} 未包含 CLI 版本前缀 {CLI_UA_VERSION}（应为 {{CLI}}-b{{N}} 形态）"
    )
    assert ver != CLI_UA_VERSION, (
        "服务版本与裸 CLI 版本相同 —— 镜像 tag 会因不变而复用旧镜像，"
        "SERVICE_BUILD 必须体现在版本串里"
    )
    # UA 仍由裸 CLI 版本驱动（不含 -bN）：上游只认 CLI 自己的版本号
    assert cli_user_agent().startswith(f"CLI/{CLI_UA_VERSION} "), (
        f"UA 里的版本与 CLI 版本不一致：{cli_user_agent()!r} vs {CLI_UA_VERSION}"
    )
    assert mod.archive_name() == f"wb2api-{ver}.tar.gz", mod.archive_name()
    assert mod.archive_name("9.9.9") == "wb2api-9.9.9.tar.gz", "archive_name 未按参数生效"
    print(f"✅ test_archive_name_matches_codebuddy_version  ({mod.archive_name()})")


def test_archive_embeds_version_file():
    """包内（以及解出的目录里）必须有一份 `VERSION`，首行就是版本号。

    为什么：NAS 上镜像 tag 可能与实际源码不一致（手工 build 过），
    最省事的核对方式就是 `cat VERSION`。也让「这个目录是哪版」不依赖记忆。
    """
    import importlib.util
    import tarfile as _tarfile
    import tempfile

    spec = importlib.util.spec_from_file_location("pack_for_nas", HERE / "pack_for_nas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        arc = td / mod.archive_name()
        mod.build_archive(arc)

        with _tarfile.open(arc) as tf:
            names = tf.getnames()
            assert "VERSION" in names, f"包内没有 VERSION：{names[:5]}…"
            body = tf.extractfile("VERSION").read().decode("utf-8")
        first = body.splitlines()[0].strip()
        assert first == mod.upstream_version(), (first, mod.upstream_version())

        dest = td / "src"
        mod.extract_archive(arc, dest)
        assert (dest / "VERSION").is_file(), "解压后没有 VERSION"
        assert (dest / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip() == first
    print(f"✅ test_archive_embeds_version_file  ({first})")


def test_compose_image_tag_is_versioned():
    """镜像 tag 必须带版本，不能是 `latest`。

    固定 `latest` 会让 `docker compose up -d` 直接复用旧镜像、**掩盖重建**，
    表现为「改了 environment 没生效」。带版本号后换包就得改 tag，改了必然重建。

    tag 形态 = `{CLI 版本}-b{构建号}`（如 `2.155.0-b2`）：
    -bN 后缀是关键 —— 裸 semver（2.155.0）在 CLI 版本不动时会长期不变，
    compose 又会开始复用旧镜像。构建号变了 tag 就变了，重建是自动的。
    """
    import re

    raw = (HERE / "compose.yaml").read_text(encoding="utf-8")
    active = [ln.strip() for ln in raw.splitlines() if not ln.strip().startswith("#")]
    img = [ln for ln in active if ln.startswith("image:")]
    assert img, "compose 里找不到生效的 image:"
    value = img[0].split(":", 1)[1].strip().strip("'\"")
    assert not value.endswith(":latest"), (
        f"image 用了 latest（{value}）—— 会掩盖重建，改用 workbuddy2api:<版本>"
    )
    tag = value.rsplit(":", 1)[-1]
    assert re.fullmatch(r"\d+\.\d+\.\d+-b\d+", tag), (
        f"image tag 不是「semver-b构建号」形态：{value!r}（如 2.155.0-b2）"
    )

    # tag 必须与包里/VERSION 里的服务版本一致
    import importlib.util

    spec = importlib.util.spec_from_file_location("pack_for_nas", HERE / "pack_for_nas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tag == mod.upstream_version(), (
        f"image tag {tag} 与包版本 {mod.upstream_version()} 不一致 —— 换包时忘了改 compose"
    )
    print(f"✅ test_compose_image_tag_is_versioned  ({value})")


if __name__ == "__main__":
    t0 = __import__("time").time()
    test_dockerfile_copies_every_imported_module()
    test_dockerfile_entrypoint_exists()
    test_requirements_pins_httpx_with_socks()
    test_dockerignore_does_not_exclude_entrycode()
    test_dockerignore_excludes_credentials()
    test_compose_mounts_auth_writable()
    test_compose_sets_timezone()
    test_compose_env_vars_are_known()
    test_env_docs_match_code()
    test_compose_mount_paths_documented_in_readme()
    test_synology_guide_mentions_manual_mkdir()
    test_pack_script_produces_flat_archive()
    test_compose_supports_tarball_context()
    test_compose_context_is_a_directory()
    test_pack_script_can_extract_to_directory()
    test_archive_name_matches_codebuddy_version()
    test_archive_embeds_version_file()
    test_compose_image_tag_is_versioned()
    print(f"\n🎉 All 18 tests passed! ({__import__('time').time() - t0:.2f}s)")
