# 本地文献库

将自行下载的文献放在此目录，Crow 每次运行时会自动筛选并纳入相关文件。适合放入知网、万方的中文论文、学位论文，以及通过机构网络获取的付费论文。

- 支持格式：`.pdf`、`.md`、`.txt`、`.docx`、`.html`；可按子目录分类。
- 建议为每个文件附一个同名元数据文件 `<文件名>.meta.toml`（例如 `论文.pdf.meta.toml`）：

```toml
title = "多无人机协同编队控制方法研究"
authors = ["张三", "李四"]
year = 2023
venue = "兵工学报"
doi = "10.xxxx/xxxx"        # 可选
language = "zh"
document_type = "article"   # article | dissertation | report | book
abstract = "……"             # 可选；不填时读取 PDF 前两页
```

- 不提供元数据时，以文件名作为标题，以正文开头作为摘要参与筛选。
- 本目录内容已被 `.gitignore` 排除，不会提交到仓库。
