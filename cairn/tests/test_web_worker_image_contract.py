"""镜像静态约束只防止配方回退，不替代候选镜像内的实际工具验收。"""

from pathlib import Path
import re
import runpy

import pytest


CONTAINER = Path(__file__).resolve().parents[2] / "container"


def test_web_image_explicitly_installs_runtime_and_web_dependencies():
    dockerfile = (CONTAINER / "Dockerfile").read_text()
    runtime = dockerfile.split("FROM worker-node-base AS worker-runtime", 1)[1]
    install = re.search(r"apt-get install -y --no-install-recommends\s+(.*?) &&", runtime, re.S)
    assert install is not None
    packages = set(install.group(1).replace("\\", " ").split())
    assert {
        "bash", "coreutils", "procps", "findutils", "sudo", "tmux", "ca-certificates",
        "curl", "git", "jq", "openssl", "dnsutils", "python3", "python3-venv",
        "python3-requests", "python3-bs4", "python3-yaml", "ncat", "nmap", "ffuf",
        "dirsearch", "sqlmap", "chromium",
    } <= packages
    assert "npm" not in packages
    assert "golang-go" not in packages
    assert "USER kali" in runtime
    assert "WORKDIR /home/kali/workspace" in runtime
    assert "ENV PLAYWRIGHT_MCP_BROWSER=chromium" in runtime
    assert "ENV PLAYWRIGHT_MCP_EXECUTABLE_PATH=/usr/bin/chromium" in runtime
    assert "ln -s /usr/bin/chromium /opt/google/chrome/chrome" in runtime
    assert "rm -rf /tmp/*" not in dockerfile


@pytest.mark.parametrize("removed", [
    "kali-linux-headless", "bloodyad", "coercer", "enum4linux-ng", "netexec", "kerbrute",
    "chisel-common-binaries", "pwncat", "cloudfox", "adb", "gitleaks", "nikto", "naabu",
    "pwntools", "pymongo", "ysoserial", "jdwp-shellifier", "PayloadsAllTheThings",
    "InternalAllTheThings", "hacktricks", "CVE-PoC", "exphub", "2023Hvv_", "Awesome-POC",
    "vulhub", "@jshookmcp/jshook", "GOPROXY",
])
def test_web_image_does_not_install_out_of_scope_tools(removed):
    assert removed not in (CONTAINER / "Dockerfile").read_text()


def test_browser_mcp_packages_are_standby_without_extra_browser_or_startup():
    dockerfile = (CONTAINER / "Dockerfile").read_text()
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g --no-audit --no-fund" in dockerfile
    assert "@playwright/mcp@0.0.81 chrome-devtools-mcp@1.9.0" in dockerfile
    assert "ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1" in dockerfile
    assert "ENV CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1" in dockerfile
    assert not re.search(r"^(CMD|ENTRYPOINT).*mcp", dockerfile, re.M | re.I)


def test_web_assets_are_pinned_verified_and_copied_without_download_archives():
    dockerfile = (CONTAINER / "Dockerfile").read_text()
    assets = dockerfile.split("FROM worker-node-base AS web-assets", 1)[1].split(
        "FROM worker-node-base AS worker-runtime", 1,
    )[0]
    assert "sha256sum -c web-assets.sha256" in assets
    assert "git clone" not in dockerfile
    assert "SecLists/8f4c1846cdb02a7024bd09decce1afc1ef5df46b" in assets
    assert "nuclei-templates/tar.gz/2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9" in assets
    assert "jwt_tool/tar.gz/3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b" in assets
    checksums = {}
    for line in (CONTAINER / "web-assets.sha256").read_text().splitlines():
        digest, name = line.split()
        assert re.fullmatch(r"[a-f0-9]{64}", digest)
        assert name not in checksums
        checksums[name] = digest
    assert set(checksums) == {
        "nuclei-templates.tar.gz", "jwt-tool.tar.gz", "LICENSE", "common.txt",
        "parameters.txt", "xss.txt", "sqli.txt",
    }
    assert "COPY --from=web-assets /assets/wordlists /opt/cairn/wordlists/web" in dockerfile
    assert "COPY --from=web-assets /assets/jwt_tool /opt/cairn/jwt_tool" in dockerfile
    assert "--chown=kali:kali /assets/nuclei-templates /home/kali/.local/nuclei-templates" in dockerfile
    assert "COPY --from=web-assets /downloads" not in dockerfile
    assert "/opt/cairn/jwt-venv/bin/python /opt/cairn/jwt_tool/jwt_tool.py" in dockerfile
    assert "--break-system-packages" not in dockerfile


def test_jwt_path_patch_changes_only_builtin_defaults(tmp_path):
    patch = runpy.run_path(str(CONTAINER / "patch-jwt-paths.py"))
    source = "\n".join(old for old, _ in patch["REPLACEMENTS"])
    source += "\nuser_path = args.keyfile\nexit(1)\n"
    tool = tmp_path / "jwt_tool.py"
    tool.write_text(source)
    patch["patch_defaults"](tool)
    expected = "\n".join(new for _, new in patch["REPLACEMENTS"])
    assert tool.read_text() == expected + "\nuser_path = args.keyfile\nexit(1)\n"
    assert list(tmp_path.iterdir()) == [tool]
    dockerfile = (CONTAINER / "Dockerfile").read_text()
    assert "python3 /tmp/cairn-patch-jwt-paths.py /opt/cairn/jwt_tool/jwt_tool.py" in dockerfile
    assert "exec /opt/cairn/jwt-venv/bin/python /opt/cairn/jwt_tool/jwt_tool.py" in dockerfile
    assert "jwt_tool.py --bare" not in dockerfile


def test_jwt_config_writer_preserves_colons_in_comments_and_values():
    import configparser
    import io

    patch = runpy.run_path(str(CONTAINER / "patch-jwt-paths.py"))
    original, replacement = next(pair for pair in patch["REPLACEMENTS"] if "ConfigParser" in pair[0])
    assert original == "config = configparser.ConfigParser(allow_no_value=True)"
    namespace = {"configparser": configparser}
    exec(replacement, namespace)
    writer = namespace["config"]
    assert writer._delimiters == ("=",)
    writer["services"] = {
        '# 配置说明：https://example.invalid/path，代理为 "host:8080"': None,
        "proxy": "127.0.0.1:8080",
        "jwksloc": "https://example.invalid/path?key=value",
    }
    output = io.StringIO()
    writer.write(output)
    reader = configparser.ConfigParser()
    reader.read_string(output.getvalue())
    assert dict(reader["services"]) == {
        "proxy": "127.0.0.1:8080",
        "jwksloc": "https://example.invalid/path?key=value",
    }


@pytest.mark.parametrize("drift", ["missing", "duplicate"])
def test_jwt_path_patch_rejects_drift_without_partial_write(tmp_path, drift):
    patch = runpy.run_path(str(CONTAINER / "patch-jwt-paths.py"))
    originals = [old for old, _ in patch["REPLACEMENTS"]]
    source = "\n".join(originals[:-1] if drift == "missing" else originals + originals[:1])
    tool = tmp_path / "jwt_tool.py"
    tool.write_text(source)
    with pytest.raises(ValueError, match="unexpected jwt_tool"):
        patch["patch_defaults"](tool)
    assert tool.read_text() == source


def test_agent_guidance_matches_web_image_scope_and_explicit_candidate_build():
    agents = (CONTAINER / "AGENTS.md").read_text()
    readme = (CONTAINER / "README.md").read_text()
    for removed_claim in ("工具齐全", "所有常见工具", "未填写", "fallback", "/usr/share/chisel"):
        assert removed_claim not in agents
    for available in ("jwt_tool", "ncat", "tmux", "register_artifact", "/opt/cairn/wordlists/web/",
                      "/home/kali/.local/nuclei-templates", "正常对照", "限制"):
        assert available in agents
    for discovery_requirement in ("实际加载的 JS", "接口文档", "表单 action/method/字段",
                                  "仅静态线索", "已观察请求", "已完成正常重放", "CSRF",
                                  "同路径不同方法", "不默认跨 TaskRun 继承登录态", "统一错误页"):
        assert discovery_requirement in agents
    assert "-t cairn-worker-container:local" not in readme
    assert "pi-web-candidate-" in readme
    assert "CAIRN_TEST_BROWSER_IMAGE" in readme
