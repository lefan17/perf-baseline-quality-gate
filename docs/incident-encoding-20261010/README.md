# 事故记录：一次编码往返损坏，与它是怎么被救回来的

**日期**：2026-10-10
**影响文件**：`dashboard/app.py`（当时约 14 KB，含 81 处中文）
**结果**：已完整恢复，内容经逐串比对确认无损

---

## 一句话

用 PowerShell 5.1 的 `Get-Content -Raw` + `Set-Content` 往返改写一个含中文的
Python 源文件，**文件被静默转成 ANSI/GBK 写盘**，中文全部损坏且不可逆解码；
因为当时项目**没有 git 仓库**也没有备份，最后是从 `__pycache__` 的 `.pyc`
里提取字符串常量重建的。

---

## 时间线

| 时刻 | 事件 |
|---|---|
| 19:45:49 | `dashboard/__pycache__/app.cpython-312.pyc` 生成（**这是后来唯一的恢复源**） |
| 19:48:02 | 用 `Get-Content -Raw` 读出、改一个函数、`Set-Content` 写回 → 文件当场损坏 |
| 19:48~ | `python -c "import dashboard.app"` 抛 `SyntaxError: invalid character '·' (U+00B7)` |
| 19:50:01 | 生成有损预览 `damaged_preview.txt`（确认损坏不可逆） |
| 19:50:31 | 从 `.pyc` 提取字符串常量，落盘 `pyc_strings.json` |
| 19:53 | 按常量重建全文，AST 解析通过 |

---

## 损坏证据

```text
$ python -c "import dashboard.app"
  File "...\dashboard\app.py", line 268
    <title>性能基线 · 质量门禁看板</title>
                     ^
SyntaxError: invalid character '·' (U+00B7)
```

以 UTF-8 读 → `UnicodeDecodeError`；以 GBK 读 → 部分字符
`illegal multibyte sequence`。**两种编码都读不回来，说明字节已经被替换，
而不是"存成了另一种编码"。**

尝试编码的完整清单（全部失败）：

```text
$ python -c "open('dashboard/app.py', encoding='utf-8').read()"
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1 in position 4312

$ python -c "open('dashboard/app.py', encoding='gbk').read()"
UnicodeDecodeError: 'gbk' codec can't decode byte 0x81 in position 1204
```

---

## 根因

```powershell
PS> $PSVersionTable.PSVersion
Major  Minor  Build  Revision
-----  -----  -----  --------
5      1      26100  9444
```

**Windows PowerShell 5.1 的 `Set-Content` 默认编码是 `Default`，
即系统 ANSI 代码页（本机为 GBK），不是 UTF-8。**
`Get-Content -Raw` 读时能解码（因为它也用同一个代码页），
但写回时中文被压成 GBK 字节；Python 再按 UTF-8 读就崩了。
**整个过程没有任何报错**，这是它最危险的地方：

```powershell
# 这行命令不会报错，但它毁文件
(Get-Content file.py -Raw) -replace 'a','b' | Set-Content file.py
```

（PowerShell 7+ 的默认编码是 UTF-8，所以这个坑只属于 5.1。
本机 `pwsh` 实际落到 5.1，是踩中的直接原因。）

---

## 恢复方法（可复用）

`.pyc` 里保存了模块的全部字符串常量，包括 docstring 和 HTML 模板。
Python 3.12 的 `.pyc` 前 16 字节是 header，之后是 `marshal` 序列化：

```python
import marshal, pathlib

data = pathlib.Path("dashboard/__pycache__/app.cpython-312.pyc").read_bytes()
code = marshal.loads(data[16:])          # 跳过 16 字节 header

strings = set()
def walk(c):
    for k in c.co_consts:
        if isinstance(k, str):
            strings.add(k)
        elif hasattr(k, "co_consts"):
            walk(k)
walk(code)
```

结果：**110 个常量、90 个唯一字符串、19 个含中文**，
涵盖全部 docstring、`_render` 里那整段 HTML f-string 模板、
以及几乎所有拼接片段。

> 关键巧合：`.pyc` 的 mtime 是 19:45:49，**早于损坏时刻 19:48:02**，
> 所以它保存的正是重构后的正确版本。
> 如果损坏发生在最后一次运行之后（例如改完就没跑过），这条路就断了。

---

## 重建后的验证（这一步不能省）

重建是"凭记忆拼回来"的动作，必须回来对账：

```python
import ast, json, pathlib

src = pathlib.Path("dashboard/app.py").read_text(encoding="utf-8")
rebuilt = {n.value for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Constant) and isinstance(n.value, str)}
original = set(json.loads(pathlib.Path("docs/incident-encoding-20261010/pyc_strings.json").read_text(encoding="utf-8")))

print(len(original), len([s for s in original if s not in rebuilt]))
# 90 2
```

**只有 2 个常量不一致，且都是有意为之：**

1. 页脚里原本硬编码「本平台的口径里 3% 以内算持平」，
   重建时改为插值 `{analysis.DEGRADATION_TOLERANCE:.0%}` ——
   这是修正（容忍带已从 3% 量到 10%），不是引入。
2. `_merge` 的 docstring 换行位置不同（内容一致，仅排版）。

另外还核对了两件事：
- **行尾**：`read_text`/`write_text` 在 Windows 上会做 LF↔CRLF 转换，
  曾把 14 213 字节的文件悄悄涨到 14 560 字节。最终用
  `read_bytes()` + `bytes.replace(b"\r\n", b"\n")` + `write_bytes()` 归零。
- **AST 解析**：`ast.parse` 通过，再跑全量测试确认行为一致。

---

## 如果重来一次

1. **先 `git init` 再动手改任何东西。** 本次事故的真正放大器不是编码，
   是没有版本控制 —— 一行 `git checkout -- dashboard/app.py` 能解决的事，
   变成了一次逆向工程。（该仓库现已初始化并提交。）
2. **不要用 PowerShell 5.1 往返改写非 ASCII 源文件。**
   本项目的变异测试与批量改写一律改用
   `pathlib.Path.read_text(encoding="utf-8")` / `write_text(..., encoding="utf-8")`，
   或编辑工具的定点替换。
3. 若必须用 PowerShell：显式指定编码，
   `Set-Content -Encoding utf8`（5.1 下仍会写 BOM，更稳妥的是 `-Encoding UTF8NoBOM`
   仅在 7+ 可用，或干脆换 Python）。

---

## 本目录文件

| 文件 | 作用 |
|---|---|
| `app.py.corrupt` | 损坏文件的原样副本（14 126 B），留作证据 |
| `damaged_preview.txt` | 损坏版的有损预览（14 526 B），可直观看到中文全成替换符 |
| `pyc_strings.json` | 从 `.pyc` 提取的 90 个唯一字符串常量，**重建的唯一依据** |

---

## 对平台的启示（为什么这条记在这里而不只是记在脑子里）

这个平台的主题是"**度量本身也会出错，而错了没人看得出来**"：
- 聚合扇出 bug 不报错、不慢，悄悄把库存算虚高 2.23 倍（见 `已知问题.md` P-4）；
- 压测脚本造出 28% 的错误率，看着像被测服务的缺陷（P-5）；
- 汇总口径有两份实现，报告和门禁给出两个不同的 P95。

**编码事故属于同一类**：不报错、测试全绿（因为测试跑的是旧字节码缓存）、
直到下次 import 才炸。区别只是这次炸掉的是工具本身。
