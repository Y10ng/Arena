# Cairn 手工知识库

本目录是 Cairn 个人版的轻量知识库入口，用于沉淀可人工维护的 markdown、yaml 和 json 文件。根目录自身对应内置知识集 `knowledge_base`；一层子目录对应同名知识集 key，例如 `web_standards/`。

建议内容：

- `tools/`：工具说明、常用参数、注意事项。
- `playbooks/`：任务流程、排查步骤、复用清单。
- `cases/`：已验证案例、关键发现、复盘记录。

知识集和文件可以在配置中心的「知识库」页创建、上传、编辑和删除。场景配置里的 `knowledge` 只能引用真实存在的知识集；被场景引用的知识集不能删除。

Markdown 文件可选使用 YAML front matter，便于人工维护标签和适用场景：

```markdown
---
title: 登录排查流程
tags: [login, target]
applies_to: [web-audit]
summary: 登录入口排查时优先检查的文件和命令
last_verified_at: 2026-06-04
---
```

使用规则：

- Dispatcher 会把本目录相对路径作为只读知识库摘要注入 prompt 和 task run runtime context。
- Front matter 是可选人工元数据；Dispatcher 只声明格式，不扫描正文、不建立索引。
- Agent 只有在相关时才应使用 `rg`、`ls` 或读取少量文件，不应把整篇知识库文档复制进最终输出。
- 本目录不是 RAG 服务，不包含 embedding、向量数据库或自动经验学习流水线。
- 涉及凭据、目标敏感信息或大输出时，应写入 artifact 或项目专属数据，不要放入通用知识库。
