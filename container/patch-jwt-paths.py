"""定点修正固定 jwt_tool 的资产路径与配置写入，不初始化配置或密钥。"""

from pathlib import Path
import sys


INPUT_DEFAULTS = """    config['input'] = {'wordlist': 'jwt-common.txt',
        'commonHeaders': 'common-headers.txt',
        'commonPayloads': 'common-payloads.txt'}"""
ABSOLUTE_INPUT_DEFAULTS = INPUT_DEFAULTS
for filename in ("jwt-common.txt", "common-headers.txt", "common-payloads.txt"):
    ABSOLUTE_INPUT_DEFAULTS = ABSOLUTE_INPUT_DEFAULTS.replace(
        f"'{filename}'", f"'/opt/cairn/jwt_tool/{filename}'",
    )
REPLACEMENTS = (
    # 上游用无值选项输出注释；仅写入器使用 =，避免 Python 3.14 将注释中的冒号当分隔符拒绝。
    ("config = configparser.ConfigParser(allow_no_value=True)",
     'config = configparser.ConfigParser(allow_no_value=True, delimiters=("=",))'),
    (INPUT_DEFAULTS, ABSOLUTE_INPUT_DEFAULTS),
    ('config[\'argvals\'][\'keyList\'] = "jwt-common.txt"',
     'config[\'argvals\'][\'keyList\'] = "/opt/cairn/jwt_tool/jwt-common.txt"'),
    ("open('jwks-common.txt', \"r\", encoding='utf-8', errors='ignore')",
     "open('/opt/cairn/jwt_tool/jwks-common.txt', \"r\", encoding='utf-8', errors='ignore')"),
)


def patch_defaults(path: Path) -> None:
    source = path.read_text()
    # 上游漂移时直接停止构建；全部匹配后才写入，不留下部分修正的脚本。
    for original, _ in REPLACEMENTS:
        if source.count(original) != 1:
            raise ValueError("unexpected jwt_tool default path source")
    for original, replacement in REPLACEMENTS:
        source = source.replace(original, replacement, 1)
    path.write_text(source)


if __name__ == "__main__":
    patch_defaults(Path(sys.argv[1]))
