# 阅读器验收文档

这是中英文混排的正文。The measure should stay comfortable; the reader must preserve punctuation、链接与 [`inline code`](https://example.com/docs)。

> 引用内容应当安静，但仍能和正文明确区分。

## Mixed content

- 第一项
  - Nested item
- 最后一项

| 项目 | 状态 |
| --- | --- |
| 文件树 | 完成 |
| 大纲 | 进行中 |

## Mixed content

重复标题必须生成稳定且不同的锚点。

### 代码

```python
def greet(name: str) -> str:
    return f"你好，{name}"

print(greet("Markdown"))
```

<script>alert("raw HTML must not execute")</script>

![Remote image should be blocked](https://example.com/tracker.png)
