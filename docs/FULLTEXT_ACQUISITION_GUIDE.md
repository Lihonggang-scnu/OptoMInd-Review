# 合法全文获取：从论文元数据到可用全文

OptoMind 把“找到论文”与“获得可用全文”分开处理。输入是一条论文元数据记录（优先 DOI，也可使用已有的 PDF 链接、OpenAlex/Semantic Scholar 标识和落地页），输出是可追溯的全文记录：原始文件、规范化文本、分块索引、来源 URL 和访问方式。

这套流程只使用开放获取来源、用户拥有权限的机构/出版社会话，以及用户自行投放的文件；它**不是**绕过付费墙的工具，也不会读取、打印或上传账号密码。

## 两条获取路径

```text
论文元数据
  │
  ├─ 已知/推断为开放获取 ──► JATS XML / TEI XML
  │                            │
  │                            ├─ Publisher / repository HTML
  │                            │
  │                            └─ Legal OA PDF（保底）
  │
  └─ 非开放获取或公共路径失败 ─► 已登录的真实 Edge（CDP）会话
                                     │
                                     ├─ 保存可见、足够完整的 publisher HTML
                                     └─ HTML 不可用时下载授权 PDF
```

### 1. 开放获取（OA）路径

候选链接由 `LiteratureResourceBuilder.fulltext_candidate_urls()` 汇总，优先级如下：

1. **JATS XML**：PMC 已知 PMCID 或 DOI→PMCID 解析；保留文章结构、章节与参考文献，最适合后续清洗与分块。
2. **TEI XML**：例如 OpenAlex 已缓存的 GROBID/TEI 内容；同样是机器友好格式。
3. **出版商或机构库 HTML**：只接受具有足够正文和章节信号的文章页。
4. **合法 OA PDF**：来自记录本身、OpenAlex、Semantic Scholar、Unpaywall、arXiv 或合法 OA PDF 后备来源；随后以 GROBID、Docling/Marker（可选）或 PyMuPDF 解析。
5. **Jina/Firecrawl 网页读取**：仅最后保底，且页面必须通过“像论文正文”的结构检查；否则只作为背景页或失败记录，不会伪装成全文。

OpenAlex 的受保护内容下载使用本地密钥池轮换；密钥只在请求时附加，**不会写入候选链接、产物或日志**。

### 2. 机构订阅路径：登录一次、逐篇复用

对于没有 OA 线索且没有直接 PDF 的记录，资源构建器会把它们从公共下载队列分离出来：公共 OA 下载可以并行；订阅文章则通过同一个真实 Edge CDP 会话**串行**处理，避免多个任务争抢同一个登录状态。

1. 用专用 Edge 用户目录启动/复用浏览器，并在可见窗口中手动完成学校图书馆、VPN、CARSI/Shibboleth 或出版社登录。
2. 浏览器保持打开；Cookie 和会话由 Edge 自己管理。下一篇文章仍连接到同一个 `http://127.0.0.1:9222` 会话。
3. 系统先检查已打开的真实文章页；如正文足够完整，优先保存 publisher HTML。若页面不是全文，再尝试同一会话中的出版社 HTML 或授权 PDF。
4. 登录页、购买页、Cloudflare/CAPTCHA 或仅摘要页会被分类为失败/人工处理线索，绝不会被当作全文。

CDP 模式的浏览器目录默认为 `literature_workspace/browser_profiles/edge_cdp_scnu/`。不要提交该目录、`storage_state.json`、下载的受版权保护全文或任何密钥文件到 Git。

## 使用方法

### A. 仅检查当前机构会话（无下载）

```powershell
py -3.11 run_edge_cdp_literature_assistant.py inspect --json
```

返回每个已打开标签页的标题、URL、文本长度和页面类型。它不会访问密码，也不会下载文件。

### B. 启动一次手动登录会话

```powershell
py -3.11 run_literature_resource_builder.py `
  --institution-login `
  --institution-browser-channel edge-cdp `
  --institution-login-url "https://libvpn.scnu.edu.cn/portal/?redirect_uri=https%3A%2F%2Flib-scnu-edu-cn-s.libvpn.scnu.edu.cn%3A20080%2F#!/login"
```

在弹出的 Edge 中手动登录，并保持浏览器窗口打开。系统不读取账号密码；Edge 的密码管理器是否自动填充完全由用户控制。

### C. 在文献资源构建时启用会话复用

```powershell
py -3.11 run_literature_resource_builder.py `
  --query-plan path\to\confirmed_query_plan.json `
  --enable-institutional-access `
  --institution-browser-channel edge-cdp `
  --institution-cdp-endpoint http://127.0.0.1:9222
```

运行产物会记录每篇文章的 `fulltext_type`、`source_url`、`access_method`、本地原始文件、规范化文本和 chunk 索引。自动失败的文章会进入人工下载清单，建议将合法下载的 PDF/HTML/XML 放入 [`user_fulltexts/`](../user_fulltexts/README.md) 后再由下游统一解析。

### D. 保存已经打开的文章页面

如果用户已经在授权 Edge 中打开了可阅读的文章页，可只保存这些页面：

```powershell
py -3.11 run_edge_cdp_literature_assistant.py save-open-pages `
  --output-dir user_fulltexts
```

仅正文长度足够且被识别为文章页的 publisher HTML 会被保存；短页面、登录页和摘要页会在结果中说明原因。

## 轻量验证

以下测试完全离线，不下载论文、不访问 API，也不会接触学校登录态：

```powershell
py -3.11 -m pytest tests/test_fulltext_acquisition_routes.py -q
py -3.11 run_edge_cdp_literature_assistant.py routes --doi 10.1002/adma.202105882
```

第一条检查“结构化全文优先、密钥不进入候选 URL、非 OA 文献进入机构串行路径”的协议；第二条只展示出版商路由建议。

## GitHub 演示页

打开 [`docs/demo/fulltext_acquisition_demo.html`](demo/fulltext_acquisition_demo.html) 可在本地交互式查看路由决策。该页面只在浏览器中模拟规则：不联网、不下载、不读取 Cookie，也不要求账号或 API key。

## 代码入口

| 职责 | 位置 |
| --- | --- |
| 元数据→候选全文路由与解析 | `optomind_research/literature_resource_builder.py` |
| OpenAlex 内容请求与密钥轮换 | `tools/academic_backends/openalex_content.py` |
| Unpaywall OA 元数据 | `tools/academic_backends/unpaywall_backend.py` |
| Edge CDP / Playwright 机构会话 | `tools/academic_backends/institutional_access_backend.py` |
| 已打开页面检查、分类和保存 | `tools/academic_backends/edge_cdp_literature_assistant.py` |
| 命令行入口 | `run_literature_resource_builder.py`、`run_edge_cdp_literature_assistant.py` |

## 合规边界

- 只使用公开 OA 链接、用户有权访问的机构/出版社页面，或用户自己合法下载的文件。
- 不使用灰色、影子或绕过付费墙的来源；`scansci-pdf` 仅保留为 OA PDF 合法后备，灰色源已禁用。
- 不把凭据、会话文件、API key、学校代理地址或版权受限全文提交到开源仓库。
