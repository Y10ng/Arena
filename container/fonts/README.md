# 默认 Worker 中文截图字体

仅包含未经修改的 **Noto Sans CJK SC Regular 2.004**，用于离线 HTTP 证据截图。
不安装完整字体集合，不依赖宿主字体挂载，也不替代渲染器的缺字检测。

- 官方项目：<https://github.com/notofonts/noto-cjk>
- 固定发行标签：`Sans2.004`
- 固定提交：`523d033d6cb47f4a80c58a35753646f5c3608a78`
- 字体路径：`Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf`
- 官方固定下载：<https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf>
- 许可来源：该提交根目录 `LICENSE`，原文保存在 `OFL.txt`；字体中的版权元数据原样保留。
- 许可：**SIL Open Font License 1.1**，允许随软件嵌入、分发；不得将字体单独销售，分发时须保留许可与版权信息。
- `SHA256SUMS` 固定字体与许可字节；Docker 构建离线校验后刷新 fontconfig 缓存。

正常构建入口仍为 `container/Dockerfile`，资源层放在现有工具与应用层之后，
不修改工具安装步骤。已有本地 Worker 仅补字体时，可以固定其本地 image ID 的标签，
以该镜像为基座执行 Dockerfile 末尾同一字体资源段，使用 `--pull=false --network=none`；
先验证候选镜像，再更新默认标签。不要为补字体重新解析浮动 Kali 基础镜像或下载工具。
