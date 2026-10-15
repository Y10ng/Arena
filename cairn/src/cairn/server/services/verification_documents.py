from __future__ import annotations

import hashlib
import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree


@dataclass(frozen=True)
class DerivedDocument:
    path: str
    content: bytes
    source_path: str


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MAX_ENTRIES = 2048
_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
_MAX_XML_BYTES = 16 * 1024 * 1024


def _xml(content: bytes) -> ElementTree.Element:
    # DOCX 不需要 DTD；拒绝实体声明，避免派生材料解析扩大资源消耗。
    declarations = content.replace(b"\x00", b"")
    if len(content) > _MAX_XML_BYTES or b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise ValueError("DOCX XML 超出解析限制或包含不支持的实体声明")
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("DOCX XML 无法解析") from exc


def extract_docx(content: bytes, original_path: str) -> list[DerivedDocument]:
    """提取有顺序的正文、表格及引用图片；原件由调用方另行完整保存。"""
    source = PurePosixPath(original_path)
    if source.is_absolute() or ".." in source.parts or "\\" in original_path:
        raise ValueError("DOCX 原件路径无效")
    # 独立目录避免不同报告的派生图片互相覆盖；调用方仍需检查原件路径冲突。
    directory = f"_derived/{hashlib.sha256(original_path.encode('utf-8')).hexdigest()[:16]}"
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 不是有效的 Office ZIP 文件") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ENTRIES or sum(entry.file_size for entry in entries) > _MAX_EXPANDED_BYTES:
            raise ValueError("DOCX 解压内容超出解析限制")
        names: set[str] = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or entry.filename in names
                or entry.flag_bits & 1
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise ValueError("DOCX 包含不安全、重复或加密的成员")
            names.add(entry.filename)
        if "word/document.xml" not in names:
            raise ValueError("DOCX 缺少 word/document.xml")

        def read_member(name: str) -> bytes:
            try:
                return archive.read(name)
            except (zipfile.BadZipFile, RuntimeError, KeyError) as exc:
                raise ValueError(f"DOCX 成员无法读取：{name}") from exc

        document = _xml(read_member("word/document.xml"))
        body = document.find(f"{_W}body")
        if body is None:
            raise ValueError("DOCX 缺少正文")
        relationships: dict[str, tuple[str, bool]] = {}
        rel_path = "word/_rels/document.xml.rels"
        if rel_path in names:
            for relationship in _xml(read_member(rel_path)).findall(f"{_REL}Relationship"):
                relation_id = relationship.get("Id")
                target = relationship.get("Target")
                if relation_id and target:
                    relationships[relation_id] = (target, relationship.get("TargetMode") == "External")

        images: dict[str, DerivedDocument] = {}

        def image_reference(relation_id: str) -> str:
            if relation_id not in relationships:
                raise ValueError("DOCX 图片缺少关联关系")
            target, external = relationships[relation_id]
            if external:
                return "[外链图片未抓取，需另行提供原始图片]"
            member = posixpath.normpath(posixpath.join("word", target))
            if not member.startswith("word/media/") or member not in names:
                raise ValueError("DOCX 图片路径无效或成员缺失")
            suffix = PurePosixPath(member).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg"}:
                return f"[内嵌图片格式 {suffix or '未知'} 暂不支持视觉读取，需提供 PNG 或 JPEG 原图]"
            image_path = f"images/{hashlib.sha256(member.encode('utf-8')).hexdigest()[:16]}{suffix}"
            if member not in images:
                images[member] = DerivedDocument(
                    path=f"{directory}/{image_path}", content=read_member(member), source_path=original_path
                )
            return f"\n\n![DOCX 内嵌图片]({image_path})\n\n"

        def paragraph(element: ElementTree.Element) -> str:
            pieces: list[str] = []
            for node in element.iter():
                if node.tag == f"{_W}t":
                    pieces.append(node.text or "")
                elif node.tag == f"{_W}tab":
                    pieces.append("\t")
                elif node.tag in {f"{_W}br", f"{_W}cr"}:
                    pieces.append("\n")
                elif node.tag == f"{_A}blip":
                    relation_id = node.get(f"{_R}embed") or node.get(f"{_R}link")
                    if relation_id:
                        pieces.append(image_reference(relation_id))
            return "".join(pieces)

        def blocks(element: ElementTree.Element) -> list[str]:
            result: list[str] = []
            for child in element:
                if child.tag == f"{_W}p":
                    result.append(paragraph(child))
                elif child.tag == f"{_W}tbl":
                    # 用逐行逐单元格正文保留顺序，不伪造表头或丢掉多段落内容。
                    rows: list[str] = ["[表格开始]"]
                    for index, row in enumerate(child.findall(f"{_W}tr"), start=1):
                        rows.append(f"[第 {index} 行]")
                        for cell_index, cell in enumerate(row.findall(f"{_W}tc"), start=1):
                            rows.append(f"[单元格 {cell_index}]\n" + "\n\n".join(blocks(cell)))
                    rows.append("[表格结束]")
                    result.append("\n\n".join(rows))
                elif child.tag == f"{_W}sdt":
                    contents = child.find(f"{_W}sdtContent")
                    if contents is not None:
                        result.extend(blocks(contents))
            return result

        text = "\n\n".join(blocks(body))
        header = (
            "# DOCX 派生阅读视图\n\n"
            f"原件：{original_path}\n\n"
            "此视图仅提取正文、表格和 PNG／JPEG 内嵌图片，不替代原件。"
            "页眉页脚、批注、修订语义、文本框及排版未完整提取；"
            "图片仍需通过实际视觉读取，不代表已经理解。外链不自动抓取。\n\n"
        )
        return [
            DerivedDocument(path=f"{directory}/document.md", content=(header + text).encode("utf-8"), source_path=original_path),
            *images.values(),
        ]
