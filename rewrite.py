#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rewrite.py —— 带上下文限定与优先级冲突消解的文本重写工具（纯标准库，单文件）

用法:
    python3 rewrite.py RULES_FILE INPUT_FILE [-o OUT] [--log LOGFILE]
                       [--max-rounds N] [--verbose]

    INPUT_FILE 为 "-" 时从标准输入读源文本。
    重写结果写 stdout（或 -o 指定文件）；应用记录写 stderr（或 --log 指定文件）。

规则文件语法（UTF-8）:
    # 注释行
    !define 标记名 文本            # 定义可在上下文条件中用 @标记名 引用的常量
    规则名[@优先级] | 匹配模式 | 上下文条件 | 替换文本

    - 匹配模式: Python 正则（re 模块语法）。
    - 上下文条件: "-" 或空表示无条件；否则为若干条件的 "&" 连接，每个条件形如:
          前=文本     匹配点之前紧邻的文本以"文本"结尾
          前!=文本    否定形式
          后=文本     匹配点之后紧邻的文本以"文本"开头
          后!=文本    否定形式
      条件文本可写 @标记名 引用 !define 定义的标记；引用未定义标记会报错并给出行号。
    - 替换文本: 支持 \\1、\\g<name> 等反向引用（re.Match.expand 语义）；
      字段为空表示删除匹配内容；整行不足 4 个字段（缺模式/缺替换）会报错并给出行号。
    - 优先级: 写在规则名后，如 货币@10；缺省为 0。同一位置多条规则可替换时，
      优先级高者先应用；优先级相同按定义顺序（行号小者优先）。

匹配与终止语义:
    1. 每轮从左到右扫描；命中某位置时先检查上下文条件，不满足则不替换。
    2. 替换后游标跳过新插入的文本（本轮内替换结果不再参与本位置匹配，防重叠），
       后续位置照常匹配。
    3. 一轮结束后若发生过替换，则再扫一轮，直到某轮无任何替换（不动点）或
       达到最大轮数（默认 100，可用 --max-rounds 调整）。
       终止保护的理由: 若某规则的替换结果仍能匹配其自身（如 块钱->块钱(真的)）
       或规则间互相循环（ab->ba, ba->ab），文本可能无限增长或振荡，
       因此用"最大轮数 + 状态重复检测"双重保护保证停机，并在日志中说明终止原因。

退出码: 0 正常；2 规则文件存在错误（所有错误带行号输出到 stderr 后不执行重写）。
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

DEFAULT_MAX_ROUNDS = 100


@dataclass
class Cond:
    side: str   # 'prev' 或 'next'
    neg: bool   # 是否为 != 形式
    text: str


@dataclass
class Rule:
    name: str
    priority: int
    order: int          # 定义顺序（行号）
    pattern: str
    regex: "re.Pattern"
    conds: List[Cond]
    repl: str
    lineno: int
    applied: int = 0    # 应用次数统计


def parse_context(ctx_str: str, markers: dict, lineno: int, errors: List[str]) -> List[Cond]:
    """解析上下文条件；非法语法或未定义标记记入 errors。"""
    ctx_str = ctx_str.strip()
    if ctx_str in ("", "-"):
        return []
    conds: List[Cond] = []
    for item in ctx_str.split("&"):
        item = item.strip()
        m = re.match(r"^(前|后)(!?=)(.*)$", item)
        if not m:
            errors.append(
                f"第{lineno}行: 无法解析的上下文条件 {item!r}"
                "（支持 前=文本 / 前!=文本 / 后=文本 / 后!=文本，多个条件用 & 连接）"
            )
            continue
        side = "prev" if m.group(1) == "前" else "next"
        neg = m.group(2) == "!="
        val = m.group(3).strip()
        if val.startswith("@"):
            key = val[1:]
            if key not in markers:
                errors.append(
                    f"第{lineno}行: 上下文条件引用了未定义的标记 '@{key}'"
                    f"（可先用 !define {key} 文本 定义）"
                )
                continue
            val = markers[key]
        if val == "":
            errors.append(f"第{lineno}行: 上下文条件 {item!r} 缺少比较文本")
            continue
        conds.append(Cond(side, neg, val))
    return conds


def load_rules(path: str) -> Tuple[List[Rule], List[str]]:
    """加载规则文件，返回 (规则列表, 错误列表)。规则按 (优先级降序, 行号升序) 排序。"""
    markers: dict = {}
    rules: List[Rule] = []
    errors: List[str] = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # 先收集 !define，使标记可定义在规则之后
    for lineno, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.startswith("!define"):
            parts = s.split(None, 2)
            if len(parts) < 3:
                errors.append(f"第{lineno}行: !define 需要 '标记名 文本' 两个参数")
            else:
                markers[parts[1]] = parts[2]

    for lineno, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("!define"):
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            missing = "匹配模式" if len(parts) < 2 else \
                      "上下文条件（可用 - 表示无条件）" if len(parts) < 3 else "替换文本"
            errors.append(
                f"第{lineno}行: 规则字段不足，缺少{missing}"
                "（格式: 规则名[@优先级] | 匹配模式 | 上下文条件 | 替换文本）"
            )
            continue
        name, pattern, ctx_str = parts[0], parts[1], parts[2]
        repl = "|".join(parts[3:]).strip()  # 替换文本里允许出现 |

        priority = 0
        if "@" in name:
            base, _, p = name.rpartition("@")
            if p.lstrip("-").isdigit():
                name, priority = base, int(p)
            else:
                errors.append(f"第{lineno}行: 规则名 {name!r} 中 '@' 后应为整数优先级")
                continue
        if not name:
            errors.append(f"第{lineno}行: 规则名为空")
            continue
        if not pattern:
            errors.append(f"第{lineno}行: 缺少匹配模式")
            continue
        try:
            regex = re.compile(pattern)
        except re.error as e:
            errors.append(f"第{lineno}行: 匹配模式 {pattern!r} 不是合法正则: {e}")
            continue

        before = len(errors)
        conds = parse_context(ctx_str, markers, lineno, errors)
        if len(errors) != before:
            continue  # 上下文条件有错，跳过该规则

        rules.append(Rule(name, priority, lineno, pattern, regex, conds, repl, lineno))

    rules.sort(key=lambda r: (-r.priority, r.order))
    return rules, errors


def conds_ok(conds: List[Cond], text: str, start: int, end: int) -> bool:
    for c in conds:
        if c.side == "prev":
            ok = text[:start].endswith(c.text)
        else:
            ok = text[end:].startswith(c.text)
        if c.neg:
            ok = not ok
        if not ok:
            return False
    return True


def rewrite(text: str, rules: List[Rule], max_rounds: int,
            verbose: bool = False) -> Tuple[str, List[str], str]:
    """执行多轮重写，返回 (结果文本, 应用记录, 终止原因)。"""
    log: List[str] = []
    seen_hashes = {hash(text)}

    for rnd in range(1, max_rounds + 1):
        pos = 0
        applied_this_round = 0
        while pos < len(text):
            hit: Optional[Tuple[Rule, "re.Match"]] = None
            for r in rules:
                m = r.regex.match(text, pos)  # 锚定在当前位置尝试
                if m is None:
                    continue
                if not conds_ok(r.conds, text, m.start(), m.end()):
                    if verbose:
                        log.append(f"第{rnd}轮 位置{pos} 规则[{r.name}] 命中 "
                                   f"{m.group(0)!r} 但上下文不满足，跳过")
                    continue
                hit = (r, m)
                break  # rules 已按优先级+定义序排序，第一个可用者即胜者
            if hit is None:
                pos += 1
                continue
            r, m = hit
            repl = m.expand(r.repl)
            text = text[:pos] + repl + text[m.end():]
            r.applied += 1
            applied_this_round += 1
            log.append(f"第{rnd}轮 位置{pos} 规则[{r.name}]: "
                       f"{m.group(0)!r} -> {repl!r}")
            pos += len(repl)          # 跳过替换结果：本轮内不再参与本位置匹配
            if m.end() == m.start():  # 空匹配保护，保证游标必前进
                pos += 1

        if applied_this_round == 0:
            return text, log, f"第{rnd - 1}轮后无新替换，达到不动点，正常终止"
        h = hash(text)
        if h in seen_hashes:
            return text, log, f"第{rnd}轮后文本状态与之前重复（规则循环），提前终止"
        seen_hashes.add(h)

    return text, log, f"达到最大轮数 {max_rounds}，强制终止（防止规则循环不停机）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="带上下文限定与优先级的文本重写工具（详见文件头部文档）")
    ap.add_argument("rules", help="规则文件路径")
    ap.add_argument("input", help="源文本文件路径，'-' 表示标准输入")
    ap.add_argument("-o", "--output", help="重写结果输出文件（默认 stdout）")
    ap.add_argument("--log", dest="logfile",
                    help="应用记录输出文件（默认 stderr）")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    help=f"最大重写轮数（默认 {DEFAULT_MAX_ROUNDS}）")
    ap.add_argument("--verbose", action="store_true",
                    help="记录因上下文不满足而跳过的命中")
    args = ap.parse_args(argv)

    if args.max_rounds < 1:
        print("错误: --max-rounds 必须 >= 1", file=sys.stderr)
        return 2

    try:
        rules, errors = load_rules(args.rules)
    except OSError as e:
        print(f"错误: 无法读取规则文件: {e}", file=sys.stderr)
        return 2
    if errors:
        print("规则文件存在错误，未执行重写:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 2

    try:
        text = sys.stdin.read() if args.input == "-" else \
            open(args.input, encoding="utf-8").read()
    except OSError as e:
        print(f"错误: 无法读取源文本: {e}", file=sys.stderr)
        return 2

    result, log, reason = rewrite(text, rules, args.max_rounds, args.verbose)

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    out.write(result)
    if not result.endswith("\n"):
        out.write("\n")
    if args.output:
        out.close()

    logf = open(args.logfile, "w", encoding="utf-8") if args.logfile else sys.stderr
    print("=== 应用记录 ===", file=logf)
    for line in log:
        print(line, file=logf)
    print("=== 统计 ===", file=logf)
    for r in rules:
        print(f"规则[{r.name}] 应用 {r.applied} 次", file=logf)
    print(f"终止原因: {reason}", file=logf)
    if args.logfile:
        logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
