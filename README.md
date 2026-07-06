# mini-max-tavily

零依赖的 Tavily 联网搜索 CLI —— 给 agent / 脚本用的命令行瑞士军刀。

基于 [Tavily API](https://tavily.com/),提供:

- **search** —— 多关键词搜索,支持并发 + 缓存
- **fetch** —— 抓 URL 正文,并发 + 去重
- **report** —— 多查询拼成调研报告,可一键保存
- **cache** —— 缓存管理(list / clear / dir)

## 特性

- ✅ **零外部依赖**,纯 Python 标准库
- ⚡ **内置缓存**:同一查询命中缓存直接返回,加速 7.6x(search)/ 78x(crawl)
- 🔒 **API key 走环境变量**,从不写入文件
- 🧪 **自带 8 项自检**(`_selftest_tavily.py`),QUICK / FULL 两档
- 🌍 **跨平台**:Windows / macOS / Linux 全跑得动
- 🤖 **agent-friendly**:结构化输出,易于脚本集成

## 安装

```bash
# 1. 把两个脚本放到任意目录(或者直接 git clone)
git clone https://github.com/Lijianpeng-Arch/mini-max-tavily.git
cd mini-max-tavily

# 2. 设置 API key
export TAVILY_API_KEY="tvly-xxxxxxxxxxxxxx"   # macOS/Linux
# Windows PowerShell:
# $env:TAVILY_API_KEY = "tvly-xxxxxxxxxxxxxx"

# 3. 跑一下自检
python _selftest_tavily.py
```

## 用法

### 搜索

```bash
# 单查询
python tavily-search.py search "Godot 4.7 新特性"

# 多查询并发
python tavily-search.py search "q1" "q2" "q3" --concurrent --cache
```

### 抓 URL 正文

```bash
python tavily-search.py fetch https://example.com https://example.org --concurrent --unique
```

### 调研报告

```bash
# 多查询拼成报告,直接打印
python tavily-search.py report "Godot 4.7" "MiniMax M3"

# 保存到文件(默认保存到 ./report_<timestamp>.md)
python tavily-search.py report "topic1" "topic2" --save
```

### 缓存管理

```bash
python tavily-search.py cache list       # 列出所有缓存
python tavily-search.py cache clear      # 清空
python tavily-search.py cache dir        # 显示缓存目录路径
```

缓存位置: `$CLAUDE_WORK_ROOT/共享区/缓存/tavily/`(环境变量可覆盖)
默认 TTL: 24 小时,原子写(.tmp + replace),SHA1 去重。

## 自检

```bash
python _selftest_tavily.py        # QUICK 档,2 项
python _selftest_tavily.py --full # FULL 档,8 项
```

返回 `exit code 0` = 全部通过,`exit code 1` = 有失败项。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TAVILY_API_KEY` | ✅ | Tavily API key,去 https://tavily.com 申请 |
| `CLAUDE_WORK_ROOT` | ❌ | 用于定位缓存目录,默认从脚本父目录反推 |

## 适用场景

- AI agent 联网调研(Claude Code / Hermes Agent / 自研 agent)
- 脚本批量抓取 + 缓存
- 个人搜索增强 / 报告生成

## 协议

MIT License —— 自由使用、修改、商用。

## 作者

[Lijianpeng-Arch](https://github.com/Lijianpeng-Arch) · 九天